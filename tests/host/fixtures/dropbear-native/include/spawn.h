#pragma once
#include_next <spawn.h>
int posix_spawnattr_setstacksize(posix_spawnattr_t *,size_t);
pid_t task_spawn(const char *,int (*)(int,char **),const posix_spawn_file_actions_t *,const posix_spawnattr_t *,char * const *,char * const *);
