# Sample data

## policies.json

Three mock auto-insurance policies, used by `tools.lookup_policy`:

| Policy # | Customer | Vehicle | VIN | Deductible |
|---|---|---|---|---|
| POL-2025-44912 | Alex Romero | 2022 Toyota Camry SE | `4T1G11AK7NU012345` | $500 |
| POL-2025-58820 | Priya Shankar | 2008 Fiat Stilo | `ZFA19200080123456` | $1000 |
| POL-2025-67114 | Marcus Chen | 2023 Ford F-150 XLT | `1FTFW1ED5NFA98765` | $0 (liability only) |

## Bring your own damage video

To make the demo end-to-end realistic, you need a short MP4 (~10–60s) of car
damage where the VIN sticker is legible in at least one frame. Options:

1. **Use your real car**: walk around your car holding the phone, pause briefly
   at any visible damage, and end by holding the door-frame VIN sticker steady
   for ~3 seconds.

2. **Use public footage**: search YouTube for "car damage walkaround insurance
   claim" — there are plenty of insurance training clips. For these, edit
   [policies.json](policies.json) so a policy's VIN matches whatever
   the video happens to show. The agent doesn't care about realism, just that
   the VIN→policy lookup succeeds.

3. **Skip VIN extraction**: edit [agent_instructions.md](../agent_instructions.md)
   step 2 to remove the VIN requirement, and have the master agent ask the
   user for a policy number directly. Use any video.

## Privacy

If you record a real car: blur or crop the license plate before uploading.
nip.io URLs are public — anyone who knows your hostname can hit it. For a
production demo, put the AKS endpoint behind an internal LB or Azure Front
Door with auth.
