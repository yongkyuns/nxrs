/* Ordinary SDK/JIT/linkage controls; no malformed pattern or exploit witness. */
#define _GNU_SOURCE
#define PCRE2_CODE_UNIT_WIDTH 8
#include <pcre2.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
int main(int argc,char **argv) {
    char version[128]={0},actual[PATH_MAX],expected[PATH_MAX]; unsigned jit=0,unicode=0; Dl_info image;
    void *selected=dlsym(RTLD_DEFAULT,"pcre2_config_8");
    if(argc!=2 || !selected || !dladdr(selected,&image) || !realpath(image.dli_fname,actual) ||
       !realpath(argv[1],expected) || strcmp(actual,expected)!=0) return 1;
    if(pcre2_config(PCRE2_CONFIG_VERSION,version)<=0 || strncmp(version,"10.49 ",6)!=0 ||
       pcre2_config(PCRE2_CONFIG_JIT,&jit)!=0 || jit!=1 ||
       pcre2_config(PCRE2_CONFIG_UNICODE,&unicode)!=0 || unicode!=1) return 2;
    const char *pattern="^/v1/cameras/[A-Za-z0-9_-]{1,64}/playback-grants$";
    int error; PCRE2_SIZE offset;
    pcre2_code *code=pcre2_compile((PCRE2_SPTR)pattern,PCRE2_ZERO_TERMINATED,0,&error,&offset,NULL);
    if(!code || pcre2_jit_compile(code,PCRE2_JIT_COMPLETE)!=0) return 3;
    pcre2_match_data *data=pcre2_match_data_create_from_pattern(code,NULL);
    pcre2_match_context *context=pcre2_match_context_create(NULL);
    pcre2_jit_stack *stack=pcre2_jit_stack_create(32768,1048576,NULL);
    if(!data || !context || !stack) return 4;
    pcre2_jit_stack_assign(context,NULL,stack);
    const char *accepted="/v1/cameras/owned_camera/playback-grants";
    const char *refused="/v1/cameras//playback-grants";
    int result=pcre2_match(code,(PCRE2_SPTR)accepted,strlen(accepted),0,PCRE2_NO_JIT,data,context)>=0 &&
        pcre2_jit_match(code,(PCRE2_SPTR)accepted,strlen(accepted),0,0,data,context)>=0 &&
        pcre2_jit_match(code,(PCRE2_SPTR)refused,strlen(refused),0,0,data,context)==PCRE2_ERROR_NOMATCH ? 0 : 5;
    pcre2_match_context_free(context);pcre2_jit_stack_free(stack);pcre2_match_data_free(data);pcre2_code_free(code);
    if(result==0) puts("PCRE2 10.49 shared probe: exact image, Unicode/JIT and ordinary matching pass");
    return result;
}
