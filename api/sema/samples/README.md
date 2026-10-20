# Samples

Canonical JSON instances, one per seeded **type** version that carries
an `examples:` block. Generated from the authored examples (never edited
by hand) and consumed by `roundtrip.py`. A type version without a sample
is silently untested by the round-trip, so its absence is recorded here.

Coverage: **23 of 38** seeded type versions have a sample.

Seeded type versions lacking a sample (no `examples:`):

- `channel.config.000`
- `glitch.000`
- `gridworks.event.problem.001`
- `gw1.tank.temp.calibration.000`
- `gw1.tank.temp.calibration.map.000`
- `ha1.params.006`
- `i2c.multichannel.dt.relay.component.gt.004`
- `layout.lite.013`
- `operating.state.sequence.000`
- `pico.flow.module.component.gt.000`
- `pico.tank.module.component.gt.011`
- `sim.pico.tank.module.component.gt.000`
- `single.reading.000`
- `spaceheat.telemetry.quantity.projection.000`
- `weather.forecast.000`
