# 03 — The `oem dust` flash-authorization token

The bootloader refuses every flash until it is unlocked with `oem dust <token>`. The token is not
looked up from a table — it is **computed from the device's config value**, so it generalizes to any
unit of the family without per-model data.

## The formula

```
token = hex8( config[0:4] XOR 0xC9ACBCC6 )
```

- `config` = the 16-byte value from `fastboot getvar config`. **Only the first 4 bytes are used.**
- `0xC9 0xAC 0xBC 0xC6` = a 4-byte constant baked into the payload at virtual address `0x4a024b9e`
  (file offset `0x24b9e`), immediately before the string `"clean whole secure store"`.
- `hex8` = lowercase, byte order preserved (`%02x` per byte, no separators).

Inside the payload the token is computed at va `0x4a018f98` and `strncmp`'d against the user input at
`0x4a019ad8`.

Reference implementation: [`tools/compute_dust_token.py`](tools/compute_dust_token.py) (has a
`--selftest` against known oracles).

### Oracles (config → token)

| Model | config (first 8 hex) | token |
|---|---|---|
| X40 `r2416` | `d97c4de6` | `10d0f120` |
| L10s `r2338` | `44268c81` | `8d8a3047` |
| D10S `r2240` | (per unit) | `18dbb75c` |
| D10S (other retailer) | (per unit) | `11c2e33d` |

The token depends only on the config value — **not** on the serial, SID, cpuid, or eFuse.

## Universal bypass tokens

The `oem dust` handler also accepts three hardcoded tokens that unlock **any** device:

```
bypass      1587cb5e      d2c41dbc
```

## The unlock state machine (and the relock trap)

The unlock flag lives at `*0x4a035288`:

| value | meaning |
|---|---|
| `2` | needs `getvar config` first |
| `1` | needs `oem dust <token>` |
| `0` | unlocked — flashing allowed |

**Trap: never issue `getvar config` between `oem dust` and the flash.** Reading the config after
unlocking drives the flag `0 → 1` and re-locks the device. The flash tooling
([`tools/run_chain.py`](tools/run_chain.py)) is written to avoid this.
