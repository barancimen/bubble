# gestaltschaft.com/bubble

The dashboard is served by the Cloudflare Worker `bubble-watch`, on path routes for
`gestaltschaft.com/bubble` and `/bubble/*`. The rest of the domain belongs to other Workers.

- `publish.sh` copies `../dashboard.html` into `public/` and deploys it. Cron runs it after each
  `bubble_watch.py` run. It reads the Cloudflare API token from `~/.cloudflare-token`.
- `src/worker.js` serves the page with the same security headers as `serve_dashboard.py`. It adds a
  red banner once the data is more than 30 hours old, so a failed run or upload is visible on the
  page.
- Setup: run `npm ci` here once, then run `./publish.sh`.
- To take the page down, run `npx wrangler delete` here.
