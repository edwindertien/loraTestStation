Vendored so the monitor works with no internet (lab networks, a Pi on a bench):

- `chart.umd.min.js` - Chart.js 4.6.1 (MIT) - https://www.chartjs.org
- `chartjs-adapter-date-fns.bundle.min.js` - chartjs-adapter-date-fns 3.0.0 with date-fns bundled (MIT)

Both fetched unmodified from the npm registry (`npm pack chart.js@4 chartjs-adapter-date-fns@3`).
To update: repeat that and replace these two files.
