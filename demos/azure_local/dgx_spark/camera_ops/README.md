# Local Camera Operations

Small Python helper for sending repeated PTZ pulse steps to the local IP camera.

Send 10 default right steps. Each step is a 1000 ms pulse, with a 2 second
pause between steps:

```bash
python -m camera_ops.pan right --steps 10
```

Use a custom step duration:

```bash
python -m camera_ops.pan left --steps 3 --step-ms 500
```

Tilt up or down with the same step controls:

```bash
python -m camera_ops.tilt up --steps 2 --step-ms 500
python -m camera_ops.tilt down --steps 2
```

`camera_ops.pan` is intentionally dumb: it sends exactly the requested pulse
count. It does not calibrate, detect walls, or read/write pan state. By default,
each pulse is an explicit `start`, local sleep for `--step-ms`, then `stop`; use
`--pulse-mode server` to delegate the whole pulse to `/wifi-ptz`. Multi-step
pan commands pause 2 seconds between steps by default; override with
`--settle-ms`. While running, it logs progress to stderr after each completed
step, including `steps_taken`.

`camera_ops.tilt` follows the same model for `up` and `down`.

Capture an 11x5 visual field grid from the current centered camera position:

```bash
python camera_ops/grid_scan.py --steps_height=5 --steps_width=11
```

The scanner moves to the top-left of the field, captures snapshots in a
serpentine path, updates the output image after every captured cell, and returns
the camera to the starting center by default. The default output is
`camera_ops/grid_scan.jpg`. The live preview is written to
`camera_ops/grid_scan_preview.jpg` during the scan and left there after the run;
`camera_ops/grid_scan_preview.html` auto-refreshes that preview image.

Preview the request without moving the camera:

```bash
python -m camera_ops.pan right --steps 10 --dry-run
```

Automatically discover both pan walls and center the camera:

```bash
python -m camera_ops.center --step-ms 1000 --max-steps-per-sweep 80
```

This scans in the first direction until a pulse produces no visual change, scans
in the reverse direction until no visual change, then moves back half of the
changed reverse-scan steps. The reverse scan must observe movement before it can
accept a no-change result as the far wall, so starting at the first wall does not
collapse the travel estimate to zero. Centering waits 3 seconds between movement
steps by default; override with `--settle-ms`.

By default, each timed step posts `start`, waits locally for the requested
duration, then posts `stop` to `/wifi-ptz` or `/bulb-ptz`:

```json
{"command": "right", "action": "start", "speed": 1}
```

With `--pulse-mode server`, it delegates duration to the server pulse endpoint:

```json
{"command": "right", "action": "pulse", "speed": 1, "duration_ms": 1000}
```
