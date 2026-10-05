import signal
import sys


def main():
    from .application import Application

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    return Application().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
