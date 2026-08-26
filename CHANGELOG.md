# Changelog

What's changed in the generator dashboard, newest first.

## 2026-08-26
- **Quick-run timers now outrank scheduled stops.** If you've asked for "run 60 minutes," a
  standing scheduled stop no longer cuts it short — the quick-run timer does the stopping.
  The suppressed stop is spent, not deferred, and shows in the schedule log as SUPPRESSED.

## 2026-08-24
- **Next scheduled start, with a Skip button.** The Control tab now shows the soonest upcoming
  scheduled start ("Today 8:45 PM, in 16m"). A one-tap **Skip** cancels just that one start —
  the schedule itself is untouched and runs normally from the next slot on. Undo re-arms it,
  and the skip survives a server restart. For the nights you shut the genset down by hand and
  don't want it waking you back up.
- Note: the App automation toggle gates the battery and temperature rules only — it has never
  stopped scheduled starts. Skip is the tool for that.

## 2026-06-16
- **Battery auto start/stop: added a cooldown.** After the generator stops — whether the rule
  stopped it, you stopped it by hand, or a safety cutoff did — the rule now waits a set "min off"
  time before it can auto-start again. This ends the short-cycling that happened when battery
  voltage sprang back up the instant the genset quit, and it stops the rule from re-cranking
  something you just shut off. Adjustable on the Battery Start/Stop tab.
- **Changelog viewer.** This page — a "What's changed" card on the Settings tab — so updates are
  here at a glance instead of buried in the repo. (Yes, its first entry includes itself.)

## 2026-06-12
- **Maintenance log.** A new Maintenance tab to jot service notes — oil changes, repairs, anything —
  each with a date and an optional engine-hours reading. It's purely a record for you: no reminders,
  no nagging.

## 2026-06-11
- **Manual fuel prime.** Opt-in on the Settings tab; once enabled, Prime and Prime + Start buttons
  appear on the Control tab. Use it to clear air from the fuel lines after the generator has run dry
  or sat unused, when a plain start would just crank without catching. The prime runs for a set time
  and stops itself.
- **Fault-logging fix.** A standing fault no longer re-logs itself every time the dashboard reconnects.

## 2026-06-10
- **Schedules limited to start/stop.** Scheduled actions are now just start or stop — the clearest,
  safest options.
- Documentation refreshed; background chart/event data moved off the main loop for smoother updates.

## 2026-06-09
- **One automation master switch.** A single "automation enabled" control now governs every auto
  start/stop rule, and the generator's own built-in auto is left off, so only one thing ever acts
  on the battery bank.
- **Battery-voltage auto start/stop.** Start the genset when house voltage sags, stop once it's
  charged back up.
- **Stats tab.** House-voltage, state-of-charge, and run-time history charts.
- **Activity log** on the History tab, plus a mobile-friendly layout.

## 2026-06-05 – 06
- **Cold-start temperature rule.** Run the heater off the generator when it gets cold, all in one
  Temperature Startup tab.
- Low-SOC auto start/stop, run-time display fixes, and a raw fault-log diagnostic.
