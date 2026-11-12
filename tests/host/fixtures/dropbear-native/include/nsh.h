#pragma once
#include "includes.h"
#define NSH_LOGIN_NONE 0
struct nsh_vtbl_s { int placeholder; };
struct console_stdio_s { struct nsh_vtbl_s cn_vtbl; };
struct console_stdio_s *nsh_newconsole(bool);
int nsh_session(struct console_stdio_s *,int,int,char **);
void nsh_exit(struct nsh_vtbl_s *,int);
