# Reviewed MicroFlow runtime capsule

`vendor/microflow` retains the small runtime from the MIT/Apache-2.0
`microflow` 0.1.3 crate. Its model compiler, nalgebra and other dependencies
remain registry-pinned in Cargo.lock. Unused packaged models and benchmark
inputs are excluded. `source.json` pins the original archive, patch order and
every retained file hash. There is no native inference backend or platform
selection in the runtime.

The three reviewed changes are:

1. TensorFlow-compatible SAME padding. Stride-two/even inputs put the odd
   padding pixel at the bottom/right. Upstream centered it, changing model
   semantics; reverting this patch fails the real Rust padding assertion.
2. Pointwise convolution borrows the input pixel and hoists filter-only
   zero-point corrections outside the pixel loop.
3. Depthwise convolution accumulates centered quantized products in one pass
   over valid pixels, avoiding repeated reductions and copies of constants.
   Requantization and activation behavior are preserved.

Hand-computed kernel tests cover asymmetric zero points, strides and boundary
padding. Both optimizations preserve all 800 recorded held-out model scores.
Integer TensorFlow Lite and MicroFlow's floating-point requantization are close
rather than bit-exact: the recorded RGB evaluation differs on one threshold
classification out of 800. See the accuracy report for the full comparison.

Run `python3 tests/detection/check_runtime_patches.py` to reconstruct the exact
capsule from the pinned archive and patches (`--archive` supports offline use).
`check_padding_control.py` removes the actual padding patch in an isolated copy
and requires its regression assertion to fail. Do not edit a registry cache or
silently change the capsule inventory. No upstream issue or PR was posted by
this task.
