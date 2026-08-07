# probe

Touch probe geometry.

The probe reports the position of the tip's centre, so the surface it touched is
**one tip radius** away — `surface = reported - tip_radius`. The standoff is a
separate thing entirely: it is how far above the surface the probe parks after a
touch, and it never enters the offset.

Run the tests with `python -m pytest -q` from the repository root.
