/* Shared hot-path contract for the frozen Rust MPSC comparison fixture.
 * Apply the same fragment to both kernels. Setup, diagnostics and reporting
 * retain normal placement. SDK queue/wait/switch paths must also be in IRAM.
 */
*(.literal.*mpsc_comparison6engine6worker* .text.*mpsc_comparison6engine6worker*)
*(.literal.*mpsc_comparison6engine5offer* .text.*mpsc_comparison6engine5offer*)
/* Std operations can be inlined into this adapter; native operations can be
 * inlined into the worker or cross the SDK translation functions below.
 */
*(.literal.*transport..ordinary_std* .text.*transport..ordinary_std*)
*(.literal.*transport..Native* .text.*transport..Native*)
*(.literal.*mpsc_comparison8arrivals4tick* .text.*mpsc_comparison8arrivals4tick*)
*(.literal.*mpsc_comparison8arrivals5start* .text.*mpsc_comparison8arrivals5start*)
*(.literal.*mpsc_comparison8arrivals7release* .text.*mpsc_comparison8arrivals7release*)
*(.literal.mp_clock32 .text.mp_clock32)
*(.literal.mp_gate_post .text.mp_gate_post .literal.mp_gate_wait .text.mp_gate_wait)
*(.literal.mp_queue_send .text.mp_queue_send .literal.mp_queue_receive .text.mp_queue_receive)
*(.literal.mp_native_ticks .text.mp_native_ticks .literal.mp_pulse_arm .text.mp_pulse_arm)
*(.literal.pulse_expired .text.pulse_expired)
/* NuttX's fixture gate uses this timeout wrapper; Zephyr's k_sem_take is
 * already covered by its native kernel IRAM policy.
 */
*(.literal.nxsem_tickwait .text.nxsem_tickwait)
