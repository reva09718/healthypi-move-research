#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "bmi323_raw.h"
#include "hpi_research_wire.h"
#include "hpi_research_policy.h"

int main(void)
{
    const uint8_t frame[14] = {0xaa,0x55, 0x00,0x80, 0xff,0xff, 0xff,0x7f,
                                          0x00,0x00, 0x00,0x10, 0x00,0xf0};
    int32_t axes[6];
    bmi323_decode_raw(frame, axes);
    const int32_t expected[6] = {-32768,-1,32767,0,4096,-4096};
    assert(memcmp(axes,expected,sizeof(axes)) == 0);
    struct hpr_stamp stamp = {.first_index=99,.first_ms=5000};
    uint8_t packet[40];
    for (unsigned id=HPR_ACCEL_X;id<=HPR_GYRO_Z;++id) {
        assert(hpr_rate_hz(id) == 25);
        assert(hpr_encode(packet,sizeof(packet),id,3,42,&stamp,axes,0,5,25)==36);
        assert(packet[1]==id && packet[2]==3 && packet[3]==5);
        assert(packet[16]==0 && packet[17]==0x80 && packet[18]==0xff && packet[19]==0xff);
    }
    /* Six-target masks must allow IMU together with all physiological targets. */
    for (unsigned mask=0; mask<64; ++mask) {
        assert(!hpr_start_busy(true,false,mask,mask,mask,!!(mask&8)));
    }
    puts("PASS: IMU endian/sign decode, six signed wire IDs, rates and six-target admission");
    return 0;
}
