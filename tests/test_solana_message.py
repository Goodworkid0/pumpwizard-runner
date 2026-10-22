import base64
import json
from pathlib import Path

import pytest
from solders.message import Message

from pumpwizard_runner.solana_message import wallet_message


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/pump-route/wallet-message-layout.json').read_text())


def meaning(message):
    h = message.header
    privileges = {
        str(key): (i < h.num_required_signatures,
            i < h.num_required_signatures - h.num_readonly_signed_accounts if i < h.num_required_signatures
            else i < len(message.account_keys) - h.num_readonly_unsigned_accounts)
        for i, key in enumerate(message.account_keys)
    }
    return dict(payer=str(message.account_keys[0]), blockhash=str(message.recent_blockhash),
        privileges=privileges, instructions=[
            (str(message.account_keys[ix.program_id_index]),
             [str(message.account_keys[index]) for index in ix.accounts], bytes(ix.data))
            for ix in message.instructions])


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['name'])
def test_message_matches_independent_wallet_compiler_without_changing_meaning(case):
    source = Message.from_bytes(base64.b64decode(case['input_message']))
    expected = base64.b64decode(case['wallet_message'])
    actual = wallet_message(source)
    assert actual == expected
    assert actual != bytes(source)  # Exercise a real rebuild, not an already sorted case.
    compiled = Message.from_bytes(actual)
    assert meaning(compiled) == meaning(source)
    assert wallet_message(compiled) == actual
