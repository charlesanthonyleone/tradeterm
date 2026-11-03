#!/bin/bash
kill -- -$(cat "$(dirname "$0")/tradeterm.pid") 2>/dev/null && echo stopped
