#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>

#define FAR
#define OK 0
#define CONFIG_DISABLE_PSEUDOFS_OPERATIONS 1
struct mutex_s { int held; };
typedef struct mutex_s mutex_t;
struct imgsensor_s { int (*init)(struct imgsensor_s *); int (*uninit)(struct imgsensor_s *); };
struct imgdata_s { int (*init)(struct imgdata_s *); int (*uninit)(struct imgdata_s *); };
struct inode { void *i_private; };
struct file { struct inode *f_inode; };
typedef struct capture_mng_s
{
  unsigned char open_num;
  mutex_t mutex;
  struct imgsensor_s *imgsensor;
  struct imgdata_s *imgdata;
} capture_mng_t;

#define IMGSENSOR_INIT(s) ((s)->init(s))
#define IMGSENSOR_UNINIT(s) ((s)->uninit(s))
#define IMGDATA_INIT(d) ((d)->init(d))
#define IMGDATA_UNINIT(d) ((d)->uninit(d))
static void nxmutex_lock(mutex_t *mutex) { assert(!mutex->held); mutex->held = 1; }
static void nxmutex_unlock(mutex_t *mutex) { assert(mutex->held); mutex->held = 0; }

static int sensor_init_result, data_init_result;
static int sensor_uninit_result, data_uninit_result;
static unsigned sensor_inits, data_inits, sensor_uninits, data_uninits;
static unsigned resource_initializes, resource_cleanups;

static int sensor_init(struct imgsensor_s *sensor)
{ (void)sensor; sensor_inits++; return sensor_init_result; }
static int data_init(struct imgdata_s *data)
{ (void)data; data_inits++; return data_init_result; }
static int sensor_uninit(struct imgsensor_s *sensor)
{ (void)sensor; sensor_uninits++; return sensor_uninit_result; }
static int data_uninit(struct imgdata_s *data)
{ (void)data; data_uninits++; return data_uninit_result; }
static void initialize_resources(capture_mng_t *cmng)
{ (void)cmng; resource_initializes++; }
static void cleanup_resources(capture_mng_t *cmng)
{ (void)cmng; resource_cleanups++; }

/* FUNCTIONS */

static void reset(void)
{
  sensor_init_result = data_init_result = 0;
  sensor_uninit_result = data_uninit_result = 0;
  sensor_inits = data_inits = sensor_uninits = data_uninits = 0;
  resource_initializes = resource_cleanups = 0;
}

static void setup(capture_mng_t *manager, struct inode *inode,
                  struct imgsensor_s *sensor, struct imgdata_s *data)
{
  *sensor = (struct imgsensor_s){ sensor_init, sensor_uninit };
  *data = (struct imgdata_s){ data_init, data_uninit };
  *manager = (capture_mng_t){ .imgsensor = sensor, .imgdata = data };
  *inode = (struct inode){ .i_private = manager };
}

static void test_open_refcount_and_successful_close(void)
{
  reset();
  capture_mng_t manager; struct inode inode; struct imgsensor_s sensor;
  struct imgdata_s data; struct file file;
  setup(&manager, &inode, &sensor, &data); file.f_inode = &inode;
  assert(capture_open(&file) == 0);
  assert(capture_open(&file) == 0);
  assert(manager.open_num == 2 && sensor_inits == 1 && data_inits == 1);
  assert(resource_initializes == 1);
  assert(capture_close(&file) == 0);
  assert(manager.open_num == 1 && resource_cleanups == 0);
  assert(capture_close(&file) == 0);
  assert(manager.open_num == 0 && resource_cleanups == 1);
  assert(sensor_uninits == 1 && data_uninits == 1);
}

static void test_failed_open_unwinds(void)
{
  reset(); capture_mng_t manager; struct inode inode; struct imgsensor_s sensor;
  struct imgdata_s data; struct file file;
  setup(&manager, &inode, &sensor, &data); file.f_inode = &inode;
  sensor_init_result = -EIO;
  assert(capture_open(&file) == -ENODEV);
  assert(manager.open_num == 0 && sensor_inits == 1 && data_inits == 0);
  assert(resource_initializes == 0 && sensor_uninits == 0 && data_uninits == 0);

  reset(); setup(&manager, &inode, &sensor, &data); file.f_inode = &inode;
  data_init_result = -EIO;
  assert(capture_open(&file) == -EIO);
  assert(manager.open_num == 0 && sensor_uninits == 1 && data_uninits == 0);
  assert(resource_initializes == 0 && resource_cleanups == 0);
}

static void test_last_close_returns_first_error_and_calls_both(void)
{
  for (int sensor_error = 0; sensor_error >= -EIO; sensor_error--)
    for (int data_error = 0; data_error >= -EINVAL; data_error--)
      {
        reset(); capture_mng_t manager; struct inode inode;
        struct imgsensor_s sensor; struct imgdata_s data; struct file file;
        setup(&manager, &inode, &sensor, &data); file.f_inode = &inode;
        assert(capture_open(&file) == 0);
        sensor_uninit_result = sensor_error;
        data_uninit_result = data_error;
        int expected = sensor_error < 0 ? sensor_error : data_error;
        assert(capture_close(&file) == expected);
        assert(manager.open_num == 0 && resource_cleanups == 1);
        assert(sensor_uninits == 1 && data_uninits == 1);
      }

}

int main(void)
{
  test_open_refcount_and_successful_close();
  test_failed_open_unwinds();
  test_last_close_returns_first_error_and_calls_both();
  return 0;
}
