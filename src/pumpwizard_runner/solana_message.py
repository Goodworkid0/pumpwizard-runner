"""Compile the reviewed legacy message with web3.js-compatible account ordering.

Wallets may rebuild a legacy transaction before signing it. Solana's Rust and
web3.js compilers sort account keys differently. Normalize BEFORE quoting and
signing, so the same reviewed bytes survive either wallet implementation.
Never normalize or relax comparisons of a wallet's signed response.
"""
from solders.instruction import CompiledInstruction
from solders.message import Message


def wallet_message(message: Message) -> bytes:
    keys = message.account_keys
    header = message.header
    signed = header.num_required_signatures
    writable_signed = signed - header.num_readonly_signed_accounts
    writable_end = len(keys) - header.num_readonly_unsigned_accounts

    def order(index):
        # web3.js uses English variant collation with lowercase first. Base58
        # is ASCII alphanumeric: compare case-insensitively, then case weights.
        address = str(keys[index])
        return address.lower(), tuple(0 if c.islower() else 1 for c in address)

    # Keep the fee payer first and never move keys across privilege groups.
    indices = [0]
    for start, end in ((1, writable_signed), (writable_signed, signed),
                       (signed, writable_end), (writable_end, len(keys))):
        indices.extend(sorted(range(start, end), key=order))
    remap = {old: new for new, old in enumerate(indices)}
    instructions = [CompiledInstruction(remap[ix.program_id_index], ix.data,
        bytes(remap[index] for index in ix.accounts)) for ix in message.instructions]
    return bytes(Message.new_with_compiled_instructions(
        signed, header.num_readonly_signed_accounts, header.num_readonly_unsigned_accounts,
        [keys[index] for index in indices], message.recent_blockhash, instructions))
