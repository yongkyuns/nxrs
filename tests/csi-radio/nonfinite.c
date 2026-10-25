/* SDK witness only: synthetic zero-energy inputs, no radio or product policy. */
static void nonfinite_tests(void)
{
    static float samples[10][13];
    struct {uint32_t before;float values[13];uint32_t after;} output;
    static const unsigned splits[]={10,7};
    static const char *names[]={"zero","underflow","finite_control"};
    for(unsigned mode=0;mode<3;mode++) {
        for(unsigned r=0;r<10;r++)for(unsigned c=0;c<13;c++)
            samples[r][c]=mode==0?0.0f:mode==1?1e-30f:(float)(r+1)*(c+1)/10.0f;
        for(unsigned split=0;split<2;split++)for(unsigned repeat=0;repeat<2;repeat++) {
            output.before=0x1234abcd;output.after=0x5a5abcbc;
            for(unsigned c=0;c<13;c++)output.values[c]=12345.0f;
            track_begin(0);
            int status=esp_radar_motion_dec_compute(13,splits[split],samples,10-splits[split],samples+splits[split],output.values);
            unsigned allocations=track_end(),finite=0;
            if(status==ESP_OK)for(unsigned c=0;c<13;c++)finite+=isfinite(output.values[c])?1:0;
            printf("CSI_NONFINITE_CASE input=%s split=%u,%u repeat=%u status=%d finite=%u allocations=%u live=%u guards=%u\n",names[mode],splits[split],10-splits[split],repeat,status,finite,allocations,live,guard_errors);
            CHECK("zero-energy native status remains successful",status==ESP_OK);
            CHECK("nonfinite output reproduced or finite control",finite==(mode==2?13:0));
            CHECK("nonfinite allocation cleanup",allocations==4&&live==0&&guard_errors==0);
            CHECK("nonfinite output guards",output.before==0x1234abcd&&output.after==0x5a5abcbc);
        }
    }
}
