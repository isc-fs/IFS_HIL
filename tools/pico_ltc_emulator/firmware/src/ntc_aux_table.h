// GENERATED from isc-fs/IFS08-CE-AMS docs/ntc_rt_table.csv — do not hand-edit.
//
// AUX voltage the AMS expects at each temperature, in the LTC's 100-uV units,
// for VREF2 = 3000 mV and the BMS_LITE 6.8 kOhm pull-up (R145 / R170). Taken
// from the manufacturer R-T appendix for the fitted Fenghua CMFB103F3950,
// i.e. the SAME source the AMS decoder uses (Core/Inc/app/ntc_table.hpp), so
// the emulate -> decode round-trip is exact by construction rather than by
// two independent models happening to agree.
#ifndef NTC_AUX_TABLE_H
#define NTC_AUX_TABLE_H
#include <stdint.h>

#define NTC_AUX_T_MIN_C  (-55)
#define NTC_AUX_T_MAX_C  (125)
#define NTC_AUX_COUNT    (181)

// index = temp_C - NTC_AUX_T_MIN_C ; value = V_aux in 100-uV units
static const uint16_t k_ntc_aux_100uV[NTC_AUX_COUNT] = {
    29654, 29637, 29618, 29598, 29577, 29554, 29531, 29506, 29479, 29451,
    29422, 29391, 29358, 29323, 29286, 29247, 29207, 29164, 29118, 29071,
    29021, 28968, 28912, 28854, 28793, 28728, 28661, 28590, 28515, 28438,
    28356, 28270, 28181, 28088, 27990, 27888, 27781, 27670, 27554, 27433,
    27307, 27177, 27040, 26899, 26752, 26600, 26442, 26278, 26109, 25934,
    25753, 25566, 25373, 25174, 24969, 24758, 24541, 24318, 24090, 23855,
    23615, 23369, 23118, 22861, 22599, 22326, 22060, 21783, 21501, 21216,
    20926, 20632, 20335, 20034, 19730, 19423, 19114, 18802, 18488, 18173,
    17857, 17539, 17221, 16902, 16583, 16264, 15947, 15630, 15313, 14998,
    14685, 14374, 14065, 13758, 13454, 13153, 12854, 12560, 12267, 11980,
    11696, 11416, 11139, 10868, 10601, 10362, 10078,  9824,  9575,  9329,
     9090,  8856,  8625,  8399,  8177,  7963,  7751,  7543,  7343,  7145,
     6952,  6765,  6581,  6403,  6229,  6059,  5895,  5732,  5575,  5422,
     5273,  5128,  4988,  4852,  4718,  4589,  4462,  4340,  4220,  4105,
     3993,  3883,  3776,  3671,  3572,  3475,  3379,  3288,  3197,  3112,
     3027,  2944,  2865,  2789,  2713,  2643,  2570,  2503,  2436,  2373,
     2309,  2249,  2188,  2131,  2074,  2020,  1966,  1916,  1866,  1819,
     1773,  1726,  1682,  1639,  1600,  1560,  1520,  1480,  1445,  1409,
     1372,
};

#endif  // NTC_AUX_TABLE_H
