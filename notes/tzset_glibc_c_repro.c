/* Proof that the "concurrent time.tzset() data race" is a glibc/TSan false positive, not a
 * CPython bug: this pure-C program (no Python, no free-threading) produces the IDENTICAL TSan
 * report as CPython's tzset_race.py.
 *
 * glibc's public tzset() serializes tzset_internal() with an internal low-level lock
 * (tzset_lock, an __libc_lock/futex) that ThreadSanitizer does not interpose, so TSan cannot
 * establish happens-before across the serialized free()/strdup() of the global tzname[] strings
 * and reports a race that cannot actually occur. The calls never crash under stress, consistent
 * with the writes really being serialized.
 *
 * Build + run (glibc 2.43 here):
 *     cc -fsanitize=thread -O1 -g tzset_glibc_c_repro.c -o tzset_glibc_c_repro
 *     TSAN_OPTIONS="halt_on_error=1 symbolize=1" DEBUGINFOD_URLS= setarch -R ./tzset_glibc_c_repro
 *
 * Observed:
 *     WARNING: ThreadSanitizer: data race
 *       #1 tzset_internal time/tzset.c:401
 *     SUMMARY: ThreadSanitizer: data race time/tzset.c:401 in tzset_internal
 *     (exit 66 = TSan detected; "done, no crash" if TSan is not halting)
 */
#include <time.h>
#include <pthread.h>
#include <stdio.h>

static void *worker(void *arg) {
    (void)arg;
    for (long i = 0; i < 300000; i++)
        tzset();
    return 0;
}

int main(void) {
    pthread_t t[4];
    for (int i = 0; i < 4; i++)
        pthread_create(&t[i], 0, worker, 0);
    for (int i = 0; i < 4; i++)
        pthread_join(t[i], 0);
    printf("C tzset: done, no crash\n");
    return 0;
}
