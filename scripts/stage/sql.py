#!/usr/bin/env python3
"""Evaluate a Python expression against the designated local synthetic tenant."""

import argparse

from stack import runtime

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("expression")
    args = parser.parse_args()
    print(runtime("sql", args.expression), end="")
