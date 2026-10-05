# OpenVela sources and evidence

This index records the evidence behind the [architecture study](README.md) and [diagram atlas](architecture-atlas.md). Implementation facts, configuration alternatives and recommendations are distinguished. All three studies use the [shared review scope](../README.md); no OpenVela migration or new nxrs RTOS target is implied.

## Exact snapshots

| Source | Snapshot | Scope |
| --- | --- | --- |
| OpenVela NuttX | [`9e79ad292fd103d3b9ef737757081a3f7fbbf9a4`](https://github.com/open-vela/nuttx/tree/9e79ad292fd103d3b9ef737757081a3f7fbbf9a4) | Kconfig memory organization: FLAT, PROTECTED/MPU, KERNEL/MMU/address environments. |
| OpenVela Bluetooth | [`c423c51e69acad244b1d44f138918cbedbc70d40`](https://github.com/open-vela/frameworks_bluetooth/tree/c423c51e69acad244b1d44f138918cbedbc70d40) | Framework/local/socket choices, service-loop configuration, SAL/VHAL, driver contract and host-client recipe. |
| Shared nxrs architecture baseline | [`5c0d6360ef5190346ddfd41aec766895800ba287`](https://github.com/yongkyuns/nxrs/tree/5c0d6360ef5190346ddfd41aec766895800ba287) | Capability/provider architecture, direct qualified Rust std device access, and the proposed concurrency/qualification baseline. |
| Historical nxrs examples | [`820536962f5bea9d71f4f9960ae0f42d33184b30`](https://github.com/yongkyuns/nxrs/tree/820536962f5bea9d71f4f9960ae0f42d33184b30) | Exact `ImuSample`/`Imu::sample` example, camera C/Rust bridge and its non-V4L2 format contract. These are evidence of boundary containment, not a competing current architecture baseline. |

The concurrency document is present on the shared nxrs baseline but describes implementation qualification as pending. The reference does not upgrade a proposal into demonstrated physical-provider or target support.

## Mechanism-to-source map

| Question | Primary evidence and what it establishes |
| --- | --- |
| Platform and device responsibilities | [OpenVela LED example](https://github.com/open-vela/docs/blob/dev/en/quickstart/development_board/STM32F411.md): application, board registration, STM32 controller support, `/dev/userleds`, `userled_set_t`, `ULEDIOC_SETALL`. Familiar file calls do not standardize the device protocol. |
| Header dependencies versus runtime dependencies | Pinned Bluetooth [`Makefile.host`](https://github.com/open-vela/frameworks_bluetooth/blob/c423c51e69acad244b1d44f138918cbedbc70d40/Makefile.host): host client/tool subset imports configuration/list headers; not proof the whole platform runs on a host. |
| Device class, buffering and acquisition | [NuttX sensor model](https://nuttx.apache.org/docs/latest/components/drivers/special/sensors/sensors_uorb.html): upper/lower halves, multi-client storage, buffered push versus proactive fetch, notification and record transfer. This is not PX4's uORB implementation. |
| Task resources and language integration | [Tasks versus threads](https://nuttx.apache.org/docs/latest/implementation/tasks_vs_threads.html) and [C++ application example](https://nuttx.apache.org/docs/latest/guides/cpp_cmake.html): separate execution stacks/task groups and configured language runtime support. |
| Deferred execution | [NuttX workqueue reference](https://nuttx.apache.org/docs/latest/reference/os/wqueue.html): kernel/user workqueue configuration and high-priority bottom-half restrictions. Worker eligibility must be checked rather than inferred from a logical driver box. |
| Protection alternatives | Pinned [`Kconfig`](https://github.com/open-vela/nuttx/blob/9e79ad292fd103d3b9ef737757081a3f7fbbf9a4/Kconfig) and [protected-build guide, NuttX 13.0.0](https://nuttx.apache.org/docs/13.0.0/guides/protected_build.html): privilege gates and build prerequisites, not a guarantee for every board or a universal heap split. |
| Framework/transport/stack/hardware contracts | Pinned Bluetooth [`README`](https://github.com/open-vela/frameworks_bluetooth/blob/c423c51e69acad244b1d44f138918cbedbc70d40/README.md), [`Kconfig`](https://github.com/open-vela/frameworks_bluetooth/blob/c423c51e69acad244b1d44f138918cbedbc70d40/Kconfig), [`bt_vhal.c`](https://github.com/open-vela/frameworks_bluetooth/blob/c423c51e69acad244b1d44f138918cbedbc70d40/service/vhal/bt_vhal.c): local/socket choices, service-loop configuration and controller interface. No one-process-per-service assumption. |
| Observability | [NuttX Task Trace](https://nuttx.apache.org/docs/latest/debugging/tasktraceuser.html): configurable scheduler/syscall/IRQ notes, collection and bounded trace storage. Enabling instrumentation has its own resource cost. |
| Nxrs capability and execution obligations | Shared-baseline [HAL architecture](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/hal-platform-architecture.md), [device access policy](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/nuttx-device-access.md) and [concurrency baseline](https://github.com/yongkyuns/nxrs/blob/5c0d6360ef5190346ddfd41aec766895800ba287/docs/concurrency-event-communication.md): target details below providers; typed bounded delivery, one owner/selection point and explicit qualification. |

## Evidence scope and qualification

Code examples were reviewed from the exact snapshots above. Rolling OpenVela `dev` and Apache NuttX `latest` documentation provides explanatory context and may change independently; the sensor, scheduling, language and trace references were checked for this comparison on 2026-10-05. The memory/Bluetooth diagram analysis was source-reviewed on 2026-10-04. These are not a release matrix or a whole-repository dependency audit.

[Diagram reproduction and recorded checks](diagrams/README.md) cover D2/SVG geometry, routes, text and local document links. They do not measure firmware timing, prove allocation freedom, establish MPU/MMU availability, or qualify hardware cancellation. Recommendations about bounded ownership, shutdown and product-level diagnostics are design lessons for nxrs, not claims that every OpenVela subsystem already implements them.
