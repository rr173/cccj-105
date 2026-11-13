"""Public notice archive pipeline (stdlib only).

Modules: config, urlnorm, robots, fetcher, htmlparse, archive, store, engine,
demo, cli, sim_html, sim_server, simctl — plus the funding-table structuring
job (fundconfig, fundgrid, fundamount, fundtable, funding, fundcli,
fundsample, funddemo), an independent entry point that consumes the archived
raw HTML into SQLite funding facts.  Two more independent jobs consume the
same read-only archive: the schedule organizer (scheduleconfig, scheduledate,
scheduleextract, schedule, schedcli, schedulesample, scheduledemo) and the
offline eligibility pre-screen (eligvalue, eligclause, eligengine,
eligibility, eligcli, eligsample, eligdemo, eligdemo_main).
"""
