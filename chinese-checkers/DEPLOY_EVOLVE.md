# 部署自进化定时任务（服务器侧）

```bash
cat > /etc/systemd/system/evolve-checkers.service <<'EOF'
[Unit]
Description=Chinese Checkers heuristic self-play evolution (nightly)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/opt/chinese-checkers
ExecStart=/opt/chinese-checkers/run_evolution.sh
# CPU-bound pure-Python self-play — cap impact on the other live games
# sharing this 2-core box instead of letting it compete at full priority.
Nice=15
CPUQuota=50%
IOSchedulingClass=idle
Environment=EVOLVE_GENERATIONS=8
Environment=EVOLVE_CANDIDATES=5
Environment=EVOLVE_GAMES=24
EOF

cat > /etc/systemd/system/evolve-checkers.timer <<'EOF'
[Unit]
Description=Run Chinese Checkers self-evolution nightly

[Timer]
OnCalendar=*-*-* 04:00:00
RandomizedDelaySec=1800
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now evolve-checkers.timer
systemctl list-timers evolve-checkers.timer
```

手动跑一次（不等到凌晨4点）：

```bash
systemctl start evolve-checkers.service
journalctl -u evolve-checkers.service -f
```

查看进化报告：

```bash
cat /opt/chinese-checkers/evolution_history/report_*.md | tail -40
```

当前冠军权重 vs 默认权重：

```bash
cat /opt/chinese-checkers/weights_champion.json
```
