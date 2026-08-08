# planner

Junction velocity planning for a machine tool path.

A path is a list of segments, each with a length in millimetres and a feed cap in
mm/s. `plan()` decides how fast the tool may be moving at each junction between
segments, honouring every segment's feed cap and the machine's single acceleration
limit, and hitting the requested speeds at the start and end of the path. `times()`
turns a planned path into per-segment durations.

The invariant that matters: between any two junctions the tool can only change
speed as fast as the acceleration limit allows over that segment's length. A plan
that violates it is not slow — it is impossible, and the controller downstream
will fault mid-cut.

Run the tests from the repository root with `pytest`.
