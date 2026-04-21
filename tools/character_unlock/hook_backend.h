#ifndef CRIMSONFORGE_HOOK_BACKEND_H
#define CRIMSONFORGE_HOOK_BACKEND_H

#ifdef __cplusplus
extern "C" {
#endif

int hook_backend_install(void* target, void* detour, void** original_out, const char** backend_name_out);
void hook_backend_shutdown(void);

#ifdef __cplusplus
}
#endif

#endif
