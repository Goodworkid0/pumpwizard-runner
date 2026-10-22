"""Validate the current build's note; --live gates public release on website publication."""
import argparse
from pumpwizard_runner import __version__
from pumpwizard_runner.release_notes import note_for, note_url, verify_live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--tag', help='Require a release tag matching the source version')
    args = parser.parse_args()
    if args.tag and args.tag != 'v' + __version__:
        raise SystemExit('Release tag does not match the source version')
    try:
        post = note_for(__version__)
        if args.live:
            verify_live(post)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print(('Live note verified: ' if args.live else 'Prepared note valid; publication not checked: ') + note_url(__version__))


if __name__ == '__main__':
    main()
