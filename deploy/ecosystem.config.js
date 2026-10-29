// PM2 config for the dashboard on a server.
//
//   pm2 start deploy/ecosystem.config.js     # start
//   pm2 save                                 # remember it across reboots
//   pm2 startup                              # once per server: start PM2 on boot
//   pm2 logs encar-dashboard                 # live log
//   pm2 restart encar-dashboard              # after git pull or editing .env
//
// Reached directly as http://<server-ip>:9191 -- open the port in the firewall
// (`ufw allow 9191/tcp`, and the cloud provider's security group if any).
// Plain HTTP: set a DASHBOARD_PASSWORD in .env (the app refuses remote
// requests without one). For HTTPS behind a domain later, see
// apache-encar.conf and change the bind back to 127.0.0.1:9191.
//
// Paths are worked out from this file, so the project can live anywhere; the
// virtualenv is expected at <project>/.venv.
//
// One process, fork mode, on purpose: the app keeps the running job (crawl /
// import) in memory, and a second instance would not see it. Never switch this
// to cluster mode or raise `instances`. Threads give it concurrency.
//
// Crawls started from the dashboard are child processes of it, so a restart
// stops a crawl in progress (it is safe: an interrupted crawl marks nothing
// sold -- just start it again).

const path = require("path");
const root = path.resolve(__dirname, "..");

module.exports = {
  apps: [
    {
      name: "encar-dashboard",
      cwd: root,
      script: path.join(root, ".venv", "bin", "gunicorn"),
      args: [
        "--bind", "0.0.0.0:9191",   // reachable as http://<server-ip>:9191
        "--workers", "1",
        "--threads", "8",
        "--timeout", "180",
        "--access-logfile", "-",
        "--error-logfile", "-",
        "encar.dashboard:app",
      ],
      interpreter: "none", // gunicorn is its own executable, not a node script
      exec_mode: "fork",
      instances: 1,
      autorestart: true,
      restart_delay: 5000,
      kill_timeout: 30000,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
    // Every 10 minutes: partners-stock car ids -> Encar -> update-car-id for
    // the sold ones (encar/partners.py). A one-shot run, so PM2 must not
    // restart it when it exits; cron_restart starts it again. Log:
    // logs/sync-sold.log (and `pm2 logs encar-sync-sold`).
    {
      name: "encar-sync-sold",
      cwd: root,
      script: path.join(root, ".venv", "bin", "python"),
      args: ["main.py", "sync-sold"],
      interpreter: "none",
      exec_mode: "fork",
      instances: 1,
      cron_restart: "*/10 * * * *",
      autorestart: false,
      env: {
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
};
