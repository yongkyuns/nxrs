#pragma once
#include <sched.h>
#include <sys/types.h>
struct task_group_s { pid_t tg_pid,tg_ppid; };
struct tcb_s { struct task_group_s *group; };
struct tcb_s *nxsched_get_tcb(pid_t);
int sched_lock(void);
int sched_unlock(void);
