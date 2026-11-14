"""Public notice archive pipeline (stdlib only).

Modules: config, urlnorm, robots, fetcher, htmlparse, archive, store, engine,
demo, cli, sim_html, sim_server, simctl — plus the funding-table structuring
job (fundconfig, fundgrid, fundamount, fundtable, funding, fundcli,
fundsample, funddemo), an independent entry point that consumes the archived
raw HTML into SQLite funding facts.  Two more independent jobs consume the
same read-only archive: the schedule organizer (scheduleconfig, scheduledate,
scheduleextract, schedule, schedcli, schedulesample, scheduledemo) and the
offline eligibility pre-screen (eligvalue, eligclause, eligengine,
eligibility, eligcli, eligsample, eligdemo, eligdemo_main) and the XLIFF 1.2
review-intake job (xliffparse, xliffintake, xliffcli, xliffsample, xliffdemo),
which binds approved translations to exact archived character spans and keeps
usable, draft and conflicting material apart.

Storage management for the raw archive lives in worklock (the shared/exclusive
work-directory lock used by collection and reclamation), reclaim (orphan
analysis, persisted plans, journalled quarantine/restore/purge), reclaimcli,
reclaimsample and reclaimdemo.  It never deletes versions, scan facts or the
results of the downstream jobs.
"""
