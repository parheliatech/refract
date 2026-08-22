/* refract_blit -- pull a PipeWire frame and put it straight into a GL
 * texture, without the copy Python forces on us.
 *
 * Why this exists, measured on an i7-7600U with three 1920x1080 screens:
 *
 *     pull samples only ......  19.7 ms/frame
 *     + upload to textures ...  27.2 ms/frame  -> 31.7 fps
 *     neither ................   0.4 ms/frame  -> 52.4 fps (vsync bound)
 *
 * Python's own overhead in the render loop is that 0.4 ms -- nothing. The
 * cost is one binding decision: PyGObject hands back GstMapInfo.data as a
 * `bytes` object, which allocates and copies 8 MB per stream per frame
 * before anything has touched a pixel. From C the mapped pointer goes
 * directly to glTexSubImage2D, so the only copy left is the unavoidable
 * one into the GPU.
 *
 * Built without GStreamer's headers on purpose: the four functions used
 * here are stable public ABI, and dlopen'ing the runtime libraries means
 * this compiles on a machine with no -dev packages installed. Everything
 * degrades to the pure-Python path if the library is missing or a symbol
 * cannot be found -- see refract/core/fastblit.py.
 */

#include <dlfcn.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include <GL/gl.h>

/* Core since GL 2.1 but not always declared in GL/gl.h; we only ever read it. */
#ifndef GL_PIXEL_UNPACK_BUFFER_BINDING
#define GL_PIXEL_UNPACK_BUFFER_BINDING 0x88EF
#endif

/* --- the bits of GStreamer's ABI we need, declared rather than included --- */

typedef struct { void *p; } GstMiniObject;
#define GST_MAP_READ 1

typedef struct {
    void     *memory;
    int       flags;
    uint8_t  *data;
    size_t    size;
    size_t    maxsize;
    void     *user_data[4];
    void     *_gst_reserved[4];
} RfMapInfo;

static void *(*p_try_pull_sample)(void *sink, uint64_t timeout);
static void *(*p_sample_get_buffer)(void *sample);
static int   (*p_buffer_map)(void *buf, RfMapInfo *info, int flags);
static void  (*p_buffer_unmap)(void *buf, RfMapInfo *info);
static void  (*p_mini_object_unref)(void *obj);
static void *(*p_sample_get_caps)(void *sample);
static void *(*p_caps_get_structure)(void *caps, unsigned int index);
static int   (*p_structure_get_int)(void *s, const char *field, int *value);

static int g_ready = 0;

int refract_blit_init(void)
{
    if (g_ready) return 1;

    void *gst = dlopen("libgstreamer-1.0.so.0", RTLD_LAZY | RTLD_GLOBAL);
    void *app = dlopen("libgstapp-1.0.so.0", RTLD_LAZY | RTLD_GLOBAL);
    if (!gst || !app) return 0;

    p_try_pull_sample   = dlsym(app, "gst_app_sink_try_pull_sample");
    p_sample_get_buffer = dlsym(gst, "gst_sample_get_buffer");
    p_buffer_map        = dlsym(gst, "gst_buffer_map");
    p_buffer_unmap      = dlsym(gst, "gst_buffer_unmap");
    p_mini_object_unref = dlsym(gst, "gst_mini_object_unref");
    /* Caps, so a frame announces its own size rather than being inferred
     * from a byte count -- see the note on refract_blit(). */
    p_sample_get_caps    = dlsym(gst, "gst_sample_get_caps");
    p_caps_get_structure = dlsym(gst, "gst_caps_get_structure");
    p_structure_get_int  = dlsym(gst, "gst_structure_get_int");

    g_ready = p_try_pull_sample && p_sample_get_buffer && p_buffer_map
              && p_buffer_unmap && p_mini_object_unref
              && p_sample_get_caps && p_caps_get_structure
              && p_structure_get_int;
    return g_ready;
}

/* Width and height straight off the sample's caps. 0 on anything unexpected,
 * which the caller treats as "cannot verify, do not upload". */
static int sample_size(void *sample, int *w, int *h)
{
    void *caps = p_sample_get_caps(sample);
    if (!caps) return 0;
    void *st = p_caps_get_structure(caps, 0);
    if (!st) return 0;
    return p_structure_get_int(st, "width", w)
        && p_structure_get_int(st, "height", h);
}

/* Returns 1 uploaded, 0 no frame waiting, negative on a problem:
 *   -1 not initialised   -2 map failed   -3 frame is not the size we expect
 *   -4 frame does not describe its own size   -5 a pixel-unpack buffer is bound
 * The caller passes the size it built the texture for. On -3, out_w/out_h
 * receive the size the frame ACTUALLY is, so Python can resize and carry on
 * without a round trip through the slow path -- which is what the laptop-panel
 * mirror needs, since a mirrored output does not announce its resolution until
 * frames flow and can change it underneath us.
 *
 * The size is read from caps, not inferred from the byte count, for two
 * reasons a byte count cannot cover: a frame that grew is still "big enough"
 * and would silently upload its first w*h*4 bytes as a skewed image, and a
 * rotation (1920x1080 -> 1080x1920) does not change the byte count at all.
 *
 * out_w/out_h may be NULL if the caller does not care.
 */
int refract_blit(void *appsink, unsigned int texture, int w, int h,
                 int *out_w, int *out_h)
{
    if (!g_ready && !refract_blit_init()) return -1;
    if (!appsink || !texture || w <= 0 || h <= 0) return -1;

    void *sample = p_try_pull_sample(appsink, 0);
    if (!sample) return 0;

    int rc = 1;
    int fw = 0, fh = 0;
    if (!sample_size(sample, &fw, &fh)) {
        p_mini_object_unref(sample);
        return -4;
    }
    if (out_w) *out_w = fw;
    if (out_h) *out_h = fh;
    if (fw != w || fh != h) {
        p_mini_object_unref(sample);
        return -3;
    }

    void *buf = p_sample_get_buffer(sample);
    RfMapInfo info;
    memset(&info, 0, sizeof(info));

    if (!buf || !p_buffer_map(buf, &info, GST_MAP_READ)) {
        rc = -2;
    } else {
        /* Caps agreed on the size; this guards a short buffer, which would
         * otherwise have glTexSubImage2D read past the mapping. */
        if (info.size < (size_t)w * (size_t)h * 4u) {
            rc = -3;
        } else {
            /* If a pixel-unpack buffer is bound, glTexSubImage2D reads
             * info.data as an OFFSET INTO THAT BUFFER instead of as a
             * pointer, and uploads garbage without erroring. moderngl only
             * binds one when asked to, so this should never fire -- but the
             * failure is silent and remote, so refuse rather than upload. */
            GLint pbo = 0;
            glGetIntegerv(GL_PIXEL_UNPACK_BUFFER_BINDING, &pbo);
            if (pbo != 0) {
                rc = -5;
            } else {
                /* Leave the binding as we found it: moderngl caches GL state
                 * and would otherwise draw with whatever we left bound. */
                GLint prev = 0, align = 0, row = 0;
                glGetIntegerv(GL_TEXTURE_BINDING_2D, &prev);
                /* Row length and alignment are context state someone else may
                 * have left set; RGBA rows are 4-byte aligned by construction,
                 * so pin both to what this upload assumes and put them back. */
                glGetIntegerv(GL_UNPACK_ALIGNMENT, &align);
                glGetIntegerv(GL_UNPACK_ROW_LENGTH, &row);
                glPixelStorei(GL_UNPACK_ALIGNMENT, 4);
                glPixelStorei(GL_UNPACK_ROW_LENGTH, 0);

                glBindTexture(GL_TEXTURE_2D, (GLuint)texture);
                glTexSubImage2D(GL_TEXTURE_2D, 0, 0, 0, w, h,
                                GL_RGBA, GL_UNSIGNED_BYTE, info.data);
                glBindTexture(GL_TEXTURE_2D, (GLuint)prev);

                glPixelStorei(GL_UNPACK_ALIGNMENT, align);
                glPixelStorei(GL_UNPACK_ROW_LENGTH, row);
            }
        }
        p_buffer_unmap(buf, &info);
    }

    p_mini_object_unref(sample);
    return rc;
}

/* Sanity hook: hand it the pointer Python thinks is a GstAppSink and get the
 * GType name back, so a wrong pointer fails loudly here instead of
 * corrupting memory later. */
const char *refract_gtype_name(void *gobject)
{
    static const char *(*p_type_name)(size_t) = NULL;

    if (!gobject) return NULL;
    if (!p_type_name) {
        void *gobj_lib = dlopen("libgobject-2.0.so.0", RTLD_LAZY | RTLD_GLOBAL);
        if (!gobj_lib) return NULL;
        p_type_name = dlsym(gobj_lib, "g_type_name");
        if (!p_type_name) return NULL;
    }
    void *gclass = *(void **)gobject;              /* GTypeInstance.g_class */
    if (!gclass) return NULL;
    return p_type_name(*(size_t *)gclass);         /* GTypeClass.g_type    */
}
