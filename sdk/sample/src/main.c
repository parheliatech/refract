#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdbool.h>
#include <unistd.h>
#include <sys/select.h>
#include <time.h>
#include <math.h>

#include "viture.h"

#define LOG(...) fprintf(stderr, __VA_ARGS__)

static float makeFloat(uint8_t *data)
{
    float value = 0;
    uint8_t tem[4];
    tem[0] = data[3];
    tem[1] = data[2];
    tem[2] = data[1];
    tem[3] = data[0];
    memcpy(&value, tem, 4);
    return value;
}

static void imuCallback(uint8_t *data, uint16_t len, uint32_t ts)
{
    // time_t rawtime;
    // time(&rawtime);
    //  LOG("imu-%ld  len:%d ts:%d\n",rawtime, len, ts);
    // for (int i = 0; i < len; i++) {

    //      LOG("%02x", data[i]);
    //      if (i == len -1) {
    //      	LOG("\n");
    //      }
    // }

    float eulerRoll = makeFloat(data);
    float eulerPitch = makeFloat(data + 4);
    float eulerYaw = makeFloat(data + 8);

    LOG("demo imuCallback data roll %f pitch %f yaw %f\n", eulerRoll, eulerPitch, eulerYaw);

    if (len >= 36)
    {
        float quaternionW = makeFloat(data + 20);
        float quaternionX = makeFloat(data + 24);
        float quaternionY = makeFloat(data + 28);
        float quaternionZ = makeFloat(data + 32);

        LOG("demo imuCallback data w %f x %f y %f z %f\n", quaternionW, quaternionX, quaternionY, quaternionZ);
    }
}

static void mcuCallback(uint16_t msgid, uint8_t *data, uint16_t len, uint32_t ts)
{
    LOG("demo mcuCallback len %d ts %d\n", len, ts);
    // for (int i = 0; i < len; i++) {
    //      LOG("%02x", data[i]);
    //      if (i == len -1) {
    //      	LOG("\n");
    //      }
    // }
}

int main()
{
    init(imuCallback, mcuCallback);
    set_imu(true);

    fd_set read_fds;
    struct timeval timeout;
    char input_buffer[256];

    while (1)
    {

        FD_ZERO(&read_fds);

        FD_SET(STDIN_FILENO, &read_fds);

        timeout.tv_sec = 0;
        timeout.tv_usec = 0;
        int result;

        int ready = select(STDIN_FILENO + 1, &read_fds, NULL, NULL, &timeout);

        if (ready == -1)
        {
            perror("select");
            exit(EXIT_FAILURE);
        }
        else if (ready > 0)
        {

            if (FD_ISSET(STDIN_FILENO, &read_fds))
            {
                if (fgets(input_buffer, sizeof(input_buffer), stdin) != NULL)
                {
                    printf("Your input is: %s\n", input_buffer);
                    if (!strncmp("imuoff", input_buffer, strlen("imuoff")))
                    {
                        result = set_imu(false);
                        printf("set_imu off: %d\n", result);
                    }
                    if (!strncmp("imuon", input_buffer, strlen("imuon")))
                    {
                        result = set_imu(true);
                        printf("set_imu on: %d\n", result);
                    }
                    if (!strncmp("quit", input_buffer, strlen("quit")))
                    {
                        printf("quit over.\n");
                        break;
                    }
                    if (!strncmp("3d", input_buffer, strlen("3d")))
                    {
                        result = set_3d(true);
                        printf("set_3d on: %d\n", result);
                    }
                    if (!strncmp("2d", input_buffer, strlen("2d")))
                    {
                        result = set_3d(false);
                        printf("set_3d off: %d\n", result);
                    }

                    if (!strncmp("get3d", input_buffer, strlen("get3d")))
                    {
                        result = get_3d_state();
                        printf("get_3D state: %d\n", result);
                    }
                    if (!strncmp("getimu", input_buffer, strlen("getimu")))
                    {
                        result = get_imu_state();
                        printf("getimu state: %d\n", result);
                    }

                    if (!strncmp("fq60", input_buffer, strlen("fq60")))
                    {
                        result = set_imu_fq(0x00);
                        printf("setfq 60: %d\n", result);
                    }
                    if (!strncmp("fq90", input_buffer, strlen("fq90")))
                    {
                        result = set_imu_fq(0x01);
                        printf("setfq 90: %d\n", result);
                    }
                    if (!strncmp("fq120", input_buffer, strlen("fq120")))
                    {
                        result = set_imu_fq(0x02);
                        printf("setfq 120: %d\n", result);
                    }
                    if (!strncmp("fq240", input_buffer, strlen("fq240")))
                    {
                        result = set_imu_fq(0x03);
                        printf("setfq 240: %d\n", result);
                    }
                    if (!strncmp("getfq", input_buffer, strlen("getfq")))
                    {
                        result = get_imu_fq();
                        printf("getfq fq: %d\n", result);
                    }
                }
                else
                {
                    perror("fgets");
                    exit(EXIT_FAILURE);
                }
            }
        }
    }
    set_imu(false);
    deinit();
    return 0;
}
