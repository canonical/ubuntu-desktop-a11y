/* T50: snap-scoped sem_open shim (the E3 sem_open fix, the enabler
 * for shipping sd_dummy). v2 - IMPLEMENTS sem_open natively.
 *
 * Why, and why v2: speech-dispatcher's dummy/generic output module
 * families create a POSIX semaphore in module_init via glibc
 * sem_open() (dummy.c:76-77, unchecked SEM_FAILED) and the module's
 * speak thread SIGSEGVs on sem_wait(NULL) - the T20 E3 finding, which
 * is why sd_dummy was stage-filtered out. v1 of this shim only
 * rewrote the NAME argument into the strict template's granted
 * sem.snap.<instance>.* namespace - and STILL failed live: glibc's
 * own sem_open implementation creates its backing file via MKNOD at
 * an internal RANDOM temp name (/dev/shm/sem.XXXXXX - verbatim AVC on
 * the T50 VM: operation="mknod" name="/dev/shm/sem.OwoID8"
 * requested_mask="c"), which no template rule can ever match and
 * whose 'c' (create) permission the template's mrwlk grant does not
 * carry, so SEM_FAILED happens regardless of the mapped final name.
 *
 * v2 therefore does NOT call libc sem_open at all: it implements the
 * operation with primitives the EXISTING template grants cover:
 *   open(mapped_path, O_(RDWR|CREAT|EXCL...), mode)  - 'w' on
 *       /dev/shm/sem.snap.<instance>.* (template.go:391) covers
 *       open-create;
 *   ftruncate(fd, sizeof(sem_t))                     - 'w' on the fd;
 *   mmap(MAP_SHARED, PROT_READ|PROT_WRITE)           - r/w on the fd;
 *   sem_init(mapped, pshared=1, value)               - plain memory.
 * All under the snap-scoped mapped name, so zero new AppArmor surface
 * (the T47 design principle). sem_unlink maps the name and calls
 * unlink(2); the template grant carries no 'd', so unlink may fail
 * with EACCES - the caller ignores it (dummy.c:78) and the 32-byte
 * sem file then leaks per module start in tmpfs (cleared at boot;
 * recorded as accepted). Already-scoped names pass through, and every
 * other semaphore API (sem_wait/sem_post/...) works on the returned
 * pointer, so the espeak path is untouched.
 *
 * Build (snapcraft launchers part override-build):
 *   gcc -shared -fPIC -O2 -Wall -o lib/sem-snap-shim.so sem-snap-shim.c
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <semaphore.h>
#include <stdarg.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#define PREFIX_MAX 128
#define NAME_MAX_REWRITE 256
#define SEM_FILE_MAX 128

static char g_prefix[PREFIX_MAX];
static int  g_prefix_done = 0;

static const char *shim_prefix(void)
{
	if (!g_prefix_done) {
		const char *inst = getenv("SNAP_INSTANCE_NAME");
		if (inst && *inst)
			snprintf(g_prefix, sizeof g_prefix, "snap.%s", inst);
		else
			/* compile-time fallback = this snap's instance name */
			snprintf(g_prefix, sizeof g_prefix,
				 "snap.ubuntu-desktop-a11y");
		g_prefix_done = 1;
	}
	return g_prefix;
}

/* Map a sem name into the snap-scoped /dev/shm path. Names already
 * scoped pass through (the leading /sem. form is rebuilt). */
static const char *shim_map(const char *name)
{
	static _Thread_local char buf[NAME_MAX_REWRITE];
	const char *prefix = shim_prefix();
	const char *p;

	if (name == NULL || *name == '\0')
		return name;
	if (strncmp(name, prefix, strlen(prefix)) == 0)
		snprintf(buf, sizeof buf, "/dev/shm/sem.%s", name);
	p = name;
	while (*p == '/')
		p++;
	if (*p == '\0')
		return name;
	snprintf(buf, sizeof buf, "/dev/shm/sem.%s.%s", prefix, p);
	return buf;
}

/* The real sem_open, re-implemented on primitives the template grants.
 * Mirrors the observable glibc contract: O_EXCL on an existing name ->
 * SEM_FAILED with EEXIST; without O_CREAT an existing file is mapped,
 * a missing one -> SEM_FAILED with ENOENT. */
sem_t *sem_open(const char *name, int oflag, ...)
{
	mode_t mode = 0;
	unsigned int value = 0;
	va_list ap;
	const char *path;
	int fd = -1;
	int created = 0;
	void *p;
	sem_t *sem;

	if (oflag & O_CREAT) {
		va_start(ap, oflag);
		mode = va_arg(ap, mode_t);
		value = va_arg(ap, unsigned int);
		va_end(ap);
	}

	path = shim_map(name);
	if (path == NULL || oflag & ~(O_CREAT | O_EXCL)) {
		/* unknown flags: refuse rather than mis-implement */
		errno = EINVAL;
		return SEM_FAILED;
	}

	if (oflag & O_CREAT) {
		fd = open(path, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC |
				   O_NOFOLLOW, mode);
		if (fd >= 0) {
			created = 1;
		} else if (errno == EEXIST) {
			if (oflag & O_EXCL) {
				errno = EEXIST;
				return SEM_FAILED;
			}
			fd = open(path, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
			if (fd < 0)
				return SEM_FAILED;
		} else {
			return SEM_FAILED;
		}
	} else {
		fd = open(path, O_RDWR | O_CLOEXEC | O_NOFOLLOW);
		if (fd < 0)
			return SEM_FAILED;
	}

	if (ftruncate(fd, sizeof(sem_t)) != 0) {
		close(fd);
		errno = ENOMEM;
		return SEM_FAILED;
	}

	p = mmap(NULL, sizeof(sem_t), PROT_READ | PROT_WRITE, MAP_SHARED,
		 fd, 0);
	close(fd);
	if (p == MAP_FAILED) {
		errno = ENOMEM;
		return SEM_FAILED;
	}

	sem = (sem_t *) p;
	if (created && sem_init(sem, 1 /* pshared */, value) != 0) {
		munmap(p, sizeof(sem_t));
		errno = ENOMEM;
		return SEM_FAILED;
	}

	return sem;
}

int sem_unlink(const char *name)
{
	return unlink(shim_map(name));
}
