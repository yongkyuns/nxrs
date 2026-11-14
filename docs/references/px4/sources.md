# PX4 sources and evidence

This index supports the [architecture study](README.md), [detailed atlas](architecture-atlas.md) and [nxrs design notes](nxrs-design-notes.md). Primary upstream implementation and documentation establish the facts; function/section names identify reviewed mechanisms, not an exhaustive audit of every file, build mode or board. The shared purpose is [architectural prior art](../README.md), not an RTOS migration.

## Exact snapshots

| Repository/reference | Snapshot | Use |
| --- | --- | --- |
| PX4/PX4-Autopilot | [`b798249a61af32c355d95decd2805a6ab4e9d9f1`](https://github.com/PX4/PX4-Autopilot/tree/b798249a61af32c355d95decd2805a6ab4e9d9f1) | Source implementation reviewed; not a tested release/build pair. |
| yongkyuns/nxrs main | [`5c0d6360ef5190346ddfd41aec766895800ba287`](https://github.com/yongkyuns/nxrs/tree/5c0d6360ef5190346ddfd41aec766895800ba287) | Application/ownership model and proposed concurrency baseline. |

PX4 Guide `main` and NuttX `latest` pages are live documents and may change independently of these source commits. Pinned code controls precise implementation claims. The nxrs concurrency proposal is on the sampled main, but its own status still says implementation qualification is pending. It must not be presented as a fully implemented event-producing HAL or universally qualified channel backend.

## Mechanism-to-source map

All paths in this table refer to the pinned PX4 snapshot. Links for each entry are defined inline in the [atlas](architecture-atlas.md); this index records what was inspected without duplicating a second full bibliography.

| Source path | Anchor / claim checked |
| --- | --- |
| `platforms/common/uORB/uORBDeviceNode.cpp` | `write`, `publish`: copy, generation, synchronous callbacks, poll notification; no local broker hop. |
| `platforms/common/uORB/uORBDeviceNode.hpp` | `copy`: depth-one/latest behavior, reader generation, bounded-history overrun, caller-buffer copy. |
| `platforms/common/uORB/SubscriptionCallback.hpp` | `SubscriptionCallbackWorkItem::call`: publisher context, count/interval gates, scheduling rather than receiver execution. |
| `platforms/common/px4_work_queue/WorkQueue.cpp` | `Add`, `Run`, stop: semaphore wait, queue locking, serial handlers, in-flight state. |
| `src/include/containers/IntrusiveQueue.hpp` | `push`/`pop`: duplicate pending insertion is suppressed; popped items can be queued again. |
| `platforms/common/px4_work_queue/WorkQueueManager.cpp` | `WorkQueueManagerRun`: worker creation, stack/priority attributes, pthread versus non-flat NuttX paths. |
| `platforms/common/px4_work_queue/ScheduledWorkItem.cpp` | Timer trampoline schedules work; delayed/periodic/absolute scheduling; cancellation. |
| `platforms/nuttx/src/px4/common/tasks.cpp` | `px4_task_spawn_cmd`, `px4_task_join`: task/kernel distinction and documented join limitations. |
| `src/drivers/imu/invensense/icm42688p/ICM42688P.cpp` | `DataReady`, FIFO state, `FIFORead`: IRQ deferral, timestamps, synchronous transfers, overflow/recovery and backup schedule. |
| `src/modules/sensors/vehicle_imu/VehicleIMU.hpp` | Integration/calibration state; ordinary accel versus callback gyro subscription; status/gap counters. |
| `src/modules/sensors/vehicle_angular_velocity/VehicleAngularVelocity.cpp` | `rate_ctrl` assignment, sensor/FIFO callback lifecycle and sample-rate configuration. |
| `src/drivers/gps/gps.cpp` | Protocol helper selection, configuration and receive/publish loop; `sensor_gnss` report. |
| `src/modules/ekf2/EKF2.cpp` | Callback registration, single/multi IMU input, supporting sample ingestion, update/output, command acknowledgment and backup timeout. |
| `src/modules/mc_rate_control/MulticopterRateControl.cpp` | Queue assignment, angular-velocity trigger, retained target, local controller calls and supporting inputs. |
| `src/modules/mc_att_control/mc_att_control_main.cpp` | `nav_and_controllers` assignment and attitude-update callback. |
| `src/modules/mc_pos_control/MulticopterPositionControl.cpp` | `nav_and_controllers` assignment and local-position callback. |
| `src/modules/control_allocator/ControlAllocator.cpp` | `rate_ctrl` assignment, torque callback, actuator publications and backup scheduling. |

The official guides supply contextual descriptions of startup/module templates, topic definition/queue defaults, delayed EKF fusion and output prediction, control allocation/output drivers, and the separate Events Interface. NuttX documentation supplies task-group versus pthread resource-sharing semantics. Recommendations about nxrs are explicitly labeled as recommendations, not PX4 facts.

<a id="evidence-scope-and-qualification"></a>

## What the evidence does and does not show

PX4 implementation paths were reviewed at the pinned snapshot on 2026-10-02; diagram execution/routing semantics were checked on 2026-10-03. The 2026-10-05 cross-system review preserves those pins and all detailed execution/topic/lifecycle findings. Live PX4 Guide and upstream NuttX pages are explanatory context, not a claim about a newer tested build.

The reference contains **14 D2 sources and SVGs**: three detailed primary execution maps, nine compact mechanism views and two IMU/GNSS propagation views. [Reproduction and recorded checks](diagrams/README.md) describe the pinned renderer, explicit connector routes, browser inspection and their limits. The [source-qualified diagram run](https://github.com/yongkyuns/nxrs/actions/runs/37092084190) covers the checked-in layout; no runtime performance is inferred from successful rendering.

**Not performed:** a PX4/NuttX firmware build, runtime execution, hardware timing/latency benchmarks, memory-allocation instrumentation, sensor fault injection, or qualification of an nxrs production HAL/channel backend. Configuration-dependent rates, stack sizes and worst-case timing are not inferred from the diagrams. The examples do not certify every PX4 module's lifecycle or every board's support.

## Inline diagram evidence

The ten [section diagrams](diagrams/inline/README.md) summarize the mechanisms cited in each section of the [main overview](README.md). Their captions state the example scope and link the supporting sources. They do not introduce a new source baseline or imply runtime/performance measurements. Lifecycle and nxrs-design diagrams distinguish review checklists from implemented guarantees.

## Developer decision examples

| Question | Additional primary evidence |
| --- | --- |
| Adding and diagnosing a component | [Module template](https://docs.px4.io/main/en/modules/module_template), [architecture](https://docs.px4.io/main/en/concept/architecture), [startup](https://docs.px4.io/main/en/concept/system_startup), and [uORB guide](https://docs.px4.io/main/en/middleware/uorb): task/work-item choices, startup and topic inspection. Exact worker/driver/consumer details still refer to the pinned source above. |

These rolling manuals were checked on 2026-10-05; they are not a newly tested firmware release matrix. Source navigation and diagnostic tables are engineering guidance derived from the named mechanisms. The shared guide's traffic rates, burst, stall and deadlines are explicitly chosen examples, not observed RTOS performance.
