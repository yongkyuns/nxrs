//! Private Linux libc bindings; filter construction and policy are Rust.
use std::io;
pub(super) fn uid() -> u32 {
    // SAFETY: geteuid takes no pointers and does not change process state.
    unsafe { libc::geteuid() }
}
fn statement(code: u32, k: u32) -> libc::sock_filter {
    libc::sock_filter {
        code: code as u16,
        jt: 0,
        jf: 0,
        k,
    }
}
fn jump(code: u32, k: u32, jt: u8, jf: u8) -> libc::sock_filter {
    libc::sock_filter {
        code: code as u16,
        jt,
        jf,
        k,
    }
}
#[cfg(target_arch = "x86_64")]
const AUDIT_ARCH: u32 = 0xc000003e; // Linux AUDIT_ARCH_X86_64.
#[cfg(target_arch = "aarch64")]
const AUDIT_ARCH: u32 = 0xc00000b7; // Linux AUDIT_ARCH_AARCH64.
fn filter() -> Vec<libc::sock_filter> {
    let mut code = vec![
        statement(
            libc::BPF_LD | libc::BPF_W | libc::BPF_ABS,
            std::mem::offset_of!(libc::seccomp_data, arch) as u32,
        ),
        jump(
            libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K,
            AUDIT_ARCH,
            1,
            0,
        ),
        statement(libc::BPF_RET | libc::BPF_K, libc::SECCOMP_RET_KILL_PROCESS),
        statement(
            libc::BPF_LD | libc::BPF_W | libc::BPF_ABS,
            std::mem::offset_of!(libc::seccomp_data, nr) as u32,
        ),
        // Reject alternate syscall tables (including x32), rather than letting
        // an unfiltered compatibility ABI reach a forbidden operation.
        jump(
            libc::BPF_JMP | libc::BPF_JGE | libc::BPF_K,
            0x40000000,
            0,
            1,
        ),
        statement(libc::BPF_RET | libc::BPF_K, libc::SECCOMP_RET_KILL_PROCESS),
    ];
    // clone3 hides flags behind a pointer. Return ENOSYS so libc's thread
    // implementation can use the inspected clone ABI; never admit an opaque
    // process-creation request. FFmpeg threads remain in the reaped thread group.
    code.extend([
        jump(
            libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K,
            libc::SYS_clone3 as u32,
            0,
            1,
        ),
        statement(
            libc::BPF_RET | libc::BPF_K,
            libc::SECCOMP_RET_ERRNO | libc::ENOSYS as u32,
        ),
        jump(
            libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K,
            libc::SYS_clone as u32,
            0,
            7,
        ),
        statement(
            libc::BPF_LD | libc::BPF_W | libc::BPF_ABS,
            std::mem::offset_of!(libc::seccomp_data, args) as u32 + 4,
        ),
        jump(libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K, 0, 1, 0),
        statement(
            libc::BPF_RET | libc::BPF_K,
            libc::SECCOMP_RET_ERRNO | libc::EPERM as u32,
        ),
        statement(
            libc::BPF_LD | libc::BPF_W | libc::BPF_ABS,
            std::mem::offset_of!(libc::seccomp_data, args) as u32,
        ),
        jump(
            libc::BPF_JMP | libc::BPF_JSET | libc::BPF_K,
            libc::CLONE_THREAD as u32,
            1,
            0,
        ),
        statement(
            libc::BPF_RET | libc::BPF_K,
            libc::SECCOMP_RET_ERRNO | libc::EPERM as u32,
        ),
        statement(libc::BPF_RET | libc::BPF_K, libc::SECCOMP_RET_ALLOW),
    ]);
    let denied = vec![
        libc::SYS_socket,
        libc::SYS_socketpair,
        libc::SYS_io_uring_setup,
        libc::SYS_io_uring_enter,
        libc::SYS_io_uring_register,
        libc::SYS_ptrace,
        libc::SYS_process_vm_readv,
        libc::SYS_process_vm_writev,
        libc::SYS_pidfd_getfd,
        libc::SYS_pidfd_send_signal,
        libc::SYS_kill,
        libc::SYS_tkill,
        libc::SYS_tgkill,
        libc::SYS_rt_sigqueueinfo,
        libc::SYS_rt_tgsigqueueinfo,
        libc::SYS_bpf,
        libc::SYS_perf_event_open,
    ];
    #[cfg(target_arch = "x86_64")]
    let denied = {
        let mut denied = denied;
        denied.extend([libc::SYS_fork, libc::SYS_vfork]);
        denied
    };
    for nr in denied {
        code.push(jump(
            libc::BPF_JMP | libc::BPF_JEQ | libc::BPF_K,
            nr as u32,
            0,
            1,
        ));
        code.push(statement(
            libc::BPF_RET | libc::BPF_K,
            libc::SECCOMP_RET_ERRNO | libc::EPERM as u32,
        ));
    }
    code.push(statement(
        libc::BPF_RET | libc::BPF_K,
        libc::SECCOMP_RET_ALLOW,
    ));
    code
}
pub(super) fn restrict() -> io::Result<()> {
    let mut code = filter();
    let program = libc::sock_fprog {
        len: u16::try_from(code.len()).map_err(|_| io::ErrorKind::InvalidInput)?,
        filter: code.as_mut_ptr(),
    };
    // SAFETY: libc uses the target's sock_fprog/sock_filter layouts. The kernel
    // synchronously copies this live, bounded array. These calls affect only
    // this single-threaded launcher and its later descendants.
    let status = unsafe {
        if libc::prctl(
            libc::PR_SET_NO_NEW_PRIVS,
            1 as libc::c_ulong,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
        ) != 0
        {
            return Err(io::Error::last_os_error());
        }
        libc::prctl(
            libc::PR_SET_SECCOMP,
            libc::SECCOMP_MODE_FILTER as libc::c_ulong,
            &program,
            0 as libc::c_ulong,
            0 as libc::c_ulong,
        )
    };
    if status != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

pub(super) fn close_inherited() -> io::Result<()> {
    // SAFETY: this launcher is single-threaded. Close only its own non-stdio
    // descriptors before opening any policy descriptors; no parent FD changes.
    if unsafe {
        libc::syscall(
            libc::SYS_close_range,
            3 as libc::c_ulong,
            u32::MAX as libc::c_ulong,
            0 as libc::c_ulong,
        )
    } != 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
