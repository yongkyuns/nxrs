/* Ordinary synchronous vendor-header ABI/loader witness; no production ABI. */
#define _GNU_SOURCE
#include <expat.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
struct counts { unsigned starts, ends, bytes; };
static void XMLCALL start(void *opaque, const XML_Char *name, const XML_Char **attrs) {
    struct counts *c=opaque; (void)name; (void)attrs; ++c->starts;
}
static void XMLCALL end(void *opaque, const XML_Char *name) {
    struct counts *c=opaque; (void)name; ++c->ends;
}
static void XMLCALL text(void *opaque, const XML_Char *data, int length) {
    struct counts *c=opaque; (void)data; if(length>0)c->bytes+=(unsigned)length;
}
int main(int argc,char **argv) {
    Dl_info image; char actual[PATH_MAX],expected[PATH_MAX];
    void *fn=dlsym(RTLD_DEFAULT,"XML_ParserCreateNS");
    if(argc!=2 || !fn || !dladdr(fn,&image) || !realpath(image.dli_fname,actual) ||
       !realpath(argv[1],expected) || strcmp(actual,expected))return 1;
    XML_Expat_Version v=XML_ExpatVersionInfo();
    if(v.major!=2 || v.minor!=9 || v.micro!=0)return 2;
    const char *input="<r xmlns='urn:nxrs:owned'><x a='1'>owned</x></r>";
    XML_Parser p=XML_ParserCreateNS(NULL,'|'); struct counts c={0,0,0};
    if(!p)return 3;
    XML_SetUserData(p,&c); XML_SetElementHandler(p,start,end); XML_SetCharacterDataHandler(p,text);
    size_t size=strlen(input); int ok=1;
    for(size_t pos=0;pos<size;pos+=7) {
        size_t count=size-pos<7?size-pos:7;
        if(XML_Parse(p,input+pos,(int)count,pos+count==size)!=XML_STATUS_OK){ok=0;break;}
    }
    ok=ok && c.starts==2 && c.ends==2 && c.bytes==5;
    XML_ParserFree(p); if(!ok)return 4;
    p=XML_ParserCreate(NULL); if(!p)return 5;
    ok=XML_Parse(p,"<r><x></r>",10,1)==XML_STATUS_ERROR;
    XML_ParserFree(p); if(!ok)return 6;
    puts("EXPAT_2_9_0_ABI_AND_LOADER_OK"); return 0;
}
