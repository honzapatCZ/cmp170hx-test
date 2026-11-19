# CMP 170HX card checker

A read-only pre-purchase check with a short, shareable report. The only supported
workflow displays opaque grades for memory, compute, NVLink, PCIe, and auxiliary
features, alongside basic card data. There is no
technical-output switch, target override, plan generation, or programming command.

## Check an installed card

On the Linux host containing the card, run from this repository:

```sh
sudo bash scripts/ffscan.sh
```

Requirements: Python 3.7 or newer (standard library only), gcc, make, matching
Linux kernel headers, sudo, and permission to load the reader module. NVIDIA's
driver and `nvidia-smi` supply optional basic data.

Example output (illustrative readings):

```text
CMP 170HX - card check
=====================

Memory grade: B
Compute grade: B
NVLink grade: A
PCIe grade: B
Auxiliary features grade: A (experimental)

Model: NVIDIA CMP 170HX
Installed memory: 65536 MiB
Temperature: 42 C
Power draw: 53 W
Driver: 610.0
Power-on hours: ~12345.0 h (recorded driver-active time)
Current GPU session: 3.5 h (since driver initialization)

Grades classify hardware configuration; functionality was not tested.
Stability was not tested.
```

Memory, compute, and NVLink grades run from A (highest) to E; PCIe uses A/B,
and auxiliary features use A/B/E. Capacity thresholds, raw binning counts,
identifiers, and modification details are omitted from the report. Missing,
invalid, or unsupported data yields an unavailable grade rather than a guess.
A grade does not guarantee usable capacity, performance, or stability.
Each category is graded independently, so missing data in one category does not
hide the others. PCIe grades describe configuration flags rather than measured
link speed. The auxiliary category is a shared experimental indicator associated
with MIG/ECC/NVDEC, rather than separate validation of each feature.

The reader performs register reads only. Temporary collection and build files
are private and removed on exit; the command saves no raw card dumps or plans.
Save the summary with ordinary shell redirection if desired. Kernel probe data
can still appear in privileged kernel logs, and the source contains the grading
logic: the opaque report is presentation masking, not source-code secrecy.

## Power-on hours

When supported, the checker reads NVIDIA's cumulative driver-active time counter
and converts seconds to hours. This is explicitly labeled as an approximation of
power-on hours: time powered without the driver is not measured. Firmware or
counter history may be incomplete. The separate current-session reading resets
when the GPU/driver is reinitialized; it is never passed off as lifetime use.

If the driver or card does not support those queries, hours remain unavailable.
The checker does not infer a card's age from host uptime, temperature, ECC errors,
or current power draw. See NVIDIA's [runtime query documentation](https://docs.nvidia.com/deploy/nvml-api/latest/api/group__nvmlDeviceQueries.html).

## Existing captures

Existing card captures can still be checked offline. Both commands print the
same limited report and create no extra files:

```sh
python -m ffcalc report cards/card.json
python -m ffcalc scan --dir collected
```

Old captures without runtime or basic driver data show those readings as
unavailable. Regenerate a live check to obtain fresh data. Previous technical CLI
commands and switches are no longer supported. Legacy analysis routines remain
in the source for internal regression coverage; their command entry points are
disabled.

## Validation

```sh
python -m unittest discover -s tests -v
```

## License and attribution

MIT; see [LICENSE](LICENSE). The underlying register map is derived from the
[kate-land GA100 research](https://blog.kate.land/posts/using-voltage-glitching-to-unlock-the-a100/).
