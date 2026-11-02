#!/usr/bin/env bash
# Records docs/demo.svg: asciinema rec --cols 150 --rows 22 -c "VENV=<venv> bash docs/demo.sh" demo.cast, then python docs/cast2svg.py demo.cast (needs pip install pyte) docs/demo.svg (a static render of the last screen; svg-term-cli also works)
export PATH="$VENV/bin:$PATH" GULLIVER_HOME="$PWD/.gulliver"
type_run() { printf '\033[1;32m$\033[0m '; for ((i=0;i<${#1};i++)); do printf '%s' "${1:i:1}"; sleep 0.02; done; printf '\n'; eval "$1"; sleep 2; }
clear
printf '\033[2m# a 5-million-row demo table, no warehouse needed\033[0m\n'
type_run "gulliver demo --events 5000000 > /dev/null && gulliver --dsn gulliver_demo.duckdb inspect | head -3"
export GULLIVER_DSN=gulliver_demo.duckdb
printf '\n\033[2m# the classic agent mistake: a self-join on a non-unique key. Rehearse on 1%%, 3%%, 10%% first:\033[0m\n'
type_run "gulliver rehearse \"select a.event_id, b.event_id from events a join events b on a.user_id = b.user_id\" | grep -E '^\| [0-9]|Output rows:|multiplies'"
printf '\n\033[2m# a filter that blocks partition pruning, rewritten with the same rows:\033[0m\n'
type_run "gulliver fix \"select event_type, count(*) from events where date(ts) = '2025-01-20' group by 1\" | head -3"
sleep 4
