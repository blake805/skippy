# gauge

Bore measurements taken off the CMM, one row per reading. Run the tests with
`python -m pytest -q` from the repository root.

`readings.csv` has three columns: `id`, `value` (inches), and `flag`.

Two rules apply to every analysis of this data, and both are easy to miss:

- **Rows flagged `suspect` are excluded.** They come from a probe that was
  drifting, and they are kept only so the run is reproducible. Counting them
  is the most common mistake made with this file.
- **Readings are raw.** The probe reads high by `CALIBRATION_OFFSET`, and that
  offset has to come off before any statistic means anything.
  `gauge.stats.summarize` already does both of these things; prefer it to
  working the numbers out again.

Nominal bore is 1.000 inches. The tolerance band for a good bore is 0.995 to
1.005 inclusive — nominal plus or minus 0.005. Both apply to the calibrated
reading, not to the raw column.
