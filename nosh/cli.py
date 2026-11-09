"""Command-line chatbot:  python -m nosh.cli   (add --demo to run a scripted conversation)"""
import sys

from .llm import make_llm
from .session import Session
from .store import Store


def main():
    store = Store()
    s = Session(store, make_llm())
    print(f"Recipe chatbot ready - {store.total:,} recipes. Type 'help' for examples, 'quit' to exit.")
    while True:
        try:
            msg = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if msg.lower() in ("quit", "exit", "q"):
            break
        if not msg:
            continue
        print("\nbot>", s.handle(msg))


if __name__ == "__main__":
    main()
