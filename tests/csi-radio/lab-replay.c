/* Test-only SDK witness. Input framing/limits and native execution only;
 * calibration, recording, ownership and replay policy remain in shared Rust. */
static float lab_inputs[4096], lab_output[256];
static bool lab_words(unsigned id, float *out, unsigned count)
{
    char line[160];
    for (unsigned base=0;base<count;base+=4) {
        if (!fgets(line,sizeof(line),stdin) || !strchr(line,'\n')) return false;
        char *at=line;
        for (unsigned i=base;i<count && i<base+4;i++) {
            while (*at==' ') at++;
            uint32_t bits=0;
            for (unsigned digit=0;digit<8;digit++) {
                unsigned ch=(unsigned char)*at++, value;
                if (ch>='0' && ch<='9') value=ch-'0';
                else if (ch>='a' && ch<='f') value=ch-'a'+10;
                else if (ch>='A' && ch<='F') value=ch-'A'+10;
                else return false;
                bits=(bits<<4)|value;
            }
            if (*at!=' ' && *at!='\r' && *at!='\n') return false;
            memcpy(out+i,&bits,4);
        }
        while (*at==' ' || *at=='\r' || *at=='\n') at++;
        if (*at) return false;
        printf("CSI_LAB_KERNEL_WORDS id=%u words=%u\n",id,base+((count-base)<4?count-base:4));
        fflush(stdout);
    }
    return true;
}
static int lab_replay(void)
{
    unsigned calls=0; char line[160];
    puts("CSI_LAB_KERNEL_READY v=2"); fflush(stdout);
    while (fgets(line,sizeof(line),stdin)) {
        if (!strcmp(line,"END\n") || !strcmp(line,"END\r\n")) {
            printf("CSI_LAB_KERNEL_END calls=%u\n",calls); return 0;
        }
        char kind,extra; unsigned id,cols,rows0,rows1;
        if (sscanf(line,"%c %u %u %u %u %c",&kind,&id,&cols,&rows0,&rows1,&extra)!=5 ||
            (kind!='M' && kind!='W') || !cols || cols>256 || rows0>4096 || rows1>4096 ||
            (kind=='M' && (cols>64 || !((uint64_t)rows0+rows1) || ((uint64_t)rows0+rows1)*cols>4096)) ||
            (kind=='W' && (rows0 || rows1)) || ++calls>50000) return 2;
        unsigned n=kind=='M'?(rows0+rows1)*cols:2*cols;
        if (!lab_words(id,lab_inputs,n)) return 3;
        int status=0; unsigned outputs=1;
        if (kind=='M') {
            status=esp_radar_motion_dec_compute(cols,rows0,(const float (*)[cols])lab_inputs,rows1,(const float (*)[cols])(lab_inputs+rows0*cols),lab_output);
            outputs=status==ESP_OK?cols:0;
        } else lab_output[0]=esp_radar_motion_dec_wander_compute(lab_inputs,lab_inputs+cols,cols);
        printf("CSI_LAB_KERNEL_RESULT id=%u status=%d words=%u bits=",id,status,outputs);
        for (unsigned i=0;i<outputs;i++) {uint32_t bits;memcpy(&bits,lab_output+i,4);printf("%08lx",(unsigned long)bits);}
        putchar('\n');fflush(stdout);
    }
    return 4;
}
