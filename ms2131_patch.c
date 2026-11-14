/*
 * MS2131 firmware patcher for the verified ZMT02/MS2131 stock image.
 *
 * This is deliberately separate from MS2130_/ms2130_patch.c. It applies only
 * the one-byte de-sharpening experiment verified on this board's firmware:
 * file offset 0xB3AA, 0x7A -> 0x00, followed by the code checksum update.
 * It does not write to a device; it reads an input file and writes an output.
 *
 * Build: cc -O2 -Wall -Wextra -o ms2131_patch ms2131_patch.c
 * Usage: ./ms2131_patch [input.bin [output.bin]] [--force]
 */

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#define FLASH_SIZE              0x80000u
#define CODE_OFFSET             0x30u
#define EXPECTED_CODE_LENGTH    0xFBA0u
#define HEADER_CHECKSUM_OFFSET  (CODE_OFFSET + EXPECTED_CODE_LENGTH)
#define CODE_CHECKSUM_OFFSET    (HEADER_CHECKSUM_OFFSET + 2u)
#define PATCH_FILE_OFFSET       0xB3AAu
#define ORIGINAL_PATCH_BYTE     0x7Au
#define PATCHED_BYTE            0x00u
#define EXPECTED_HEADER_SUM     0x25F7u
#define EXPECTED_CODE_SUM       0x9E01u

static uint16_t read_be16(const uint8_t *p)
{
    return (uint16_t)(((uint16_t)p[0] << 8) | p[1]);
}

static void write_be16(uint8_t *p, uint16_t value)
{
    p[0] = (uint8_t)(value >> 8);
    p[1] = (uint8_t)value;
}

static uint16_t calculate_header_sum(const uint8_t *data)
{
    uint32_t sum = 0;
    unsigned i;

    for (i = 0x02; i < CODE_OFFSET; ++i) {
        if (i < 0x0C || i > 0x0F)
            sum += data[i];
    }
    return (uint16_t)(sum & 0xFFFFu);
}

static uint16_t calculate_code_sum(const uint8_t *data, size_t length)
{
    uint32_t sum = 0;
    size_t i;

    for (i = 0; i < length; ++i)
        sum += data[i];
    return (uint16_t)(sum & 0xFFFFu);
}

static int same_file(const char *input, const char *output)
{
    struct stat in_stat;
    struct stat out_stat;

    if (strcmp(input, output) == 0)
        return 1;
    if (stat(input, &in_stat) == 0 && stat(output, &out_stat) == 0 &&
        in_stat.st_dev == out_stat.st_dev && in_stat.st_ino == out_stat.st_ino)
        return 1;
    return 0;
}

int main(int argc, char **argv)
{
    const char *input_path = "ZMT02_MS2131_VER1.0_25082718_2541_SPI5E3213_ZB25WD40_stock.bin";
    const char *output_path = "ZMT02_MS2131_VER1.0_25082718_2541_SPI5E3213_ZB25WD40_deblur_candidate.bin";
    int force = 0;
    int positional = 0;
    int i;
    FILE *input = NULL;
    FILE *output = NULL;
    struct stat output_stat;
    long file_size;
    uint8_t *firmware = NULL;
    uint16_t code_length;
    uint16_t header_sum;
    uint16_t code_sum;
    int result = 1;

    for (i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--force") == 0 || strcmp(argv[i], "-f") == 0) {
            force = 1;
        } else if (argv[i][0] == '-') {
            fprintf(stderr, "Unknown option: %s\n", argv[i]);
            fprintf(stderr, "Usage: %s [input.bin [output.bin]] [--force]\n", argv[0]);
            return 2;
        } else if (positional == 0) {
            input_path = argv[i];
            ++positional;
        } else if (positional == 1) {
            output_path = argv[i];
            ++positional;
        } else {
            fprintf(stderr, "Too many file arguments.\n");
            return 2;
        }
    }

    if (same_file(input_path, output_path)) {
        fprintf(stderr, "Refusing to overwrite the input firmware.\n");
        return 2;
    }
    if (!force && stat(output_path, &output_stat) == 0) {
        fprintf(stderr, "Output already exists: %s\nUse --force to replace it.\n", output_path);
        return 2;
    }

    input = fopen(input_path, "rb");
    if (!input) {
        fprintf(stderr, "Cannot open input '%s': %s\n", input_path, strerror(errno));
        goto cleanup;
    }
    if (fseek(input, 0, SEEK_END) != 0 || (file_size = ftell(input)) < 0 ||
        fseek(input, 0, SEEK_SET) != 0) {
        fprintf(stderr, "Cannot determine input file size.\n");
        goto cleanup;
    }
    if ((unsigned long)file_size != FLASH_SIZE) {
        fprintf(stderr, "Unexpected file size: %ld bytes (expected %u-byte SPI dump).\n",
                file_size, FLASH_SIZE);
        goto cleanup;
    }

    firmware = (uint8_t *)malloc(FLASH_SIZE);
    if (!firmware) {
        fprintf(stderr, "Out of memory.\n");
        goto cleanup;
    }
    if (fread(firmware, 1, FLASH_SIZE, input) != FLASH_SIZE) {
        fprintf(stderr, "Failed to read the complete input file.\n");
        goto cleanup;
    }
    fclose(input);
    input = NULL;

    if (firmware[0] != 0x3C || firmware[1] != 0xC3) {
        fprintf(stderr, "Unexpected firmware magic.\n");
        goto cleanup;
    }
    code_length = read_be16(firmware + 0x02);
    if (code_length != EXPECTED_CODE_LENGTH) {
        fprintf(stderr, "Unexpected code length: 0x%04X (expected 0x%04X).\n",
                code_length, EXPECTED_CODE_LENGTH);
        goto cleanup;
    }
    header_sum = calculate_header_sum(firmware);
    if (header_sum != EXPECTED_HEADER_SUM ||
        read_be16(firmware + HEADER_CHECKSUM_OFFSET) != EXPECTED_HEADER_SUM) {
        fprintf(stderr, "Header checksum mismatch; refusing to patch.\n");
        goto cleanup;
    }
    code_sum = calculate_code_sum(firmware + CODE_OFFSET, code_length);
    if (code_sum != EXPECTED_CODE_SUM ||
        read_be16(firmware + CODE_CHECKSUM_OFFSET) != EXPECTED_CODE_SUM) {
        fprintf(stderr, "Code checksum mismatch; this is not the supported stock image.\n");
        goto cleanup;
    }
    if (firmware[PATCH_FILE_OFFSET] != ORIGINAL_PATCH_BYTE) {
        fprintf(stderr, "Unexpected byte at 0x%X: 0x%02X (expected 0x%02X).\n",
                PATCH_FILE_OFFSET, firmware[PATCH_FILE_OFFSET], ORIGINAL_PATCH_BYTE);
        goto cleanup;
    }

    firmware[PATCH_FILE_OFFSET] = PATCHED_BYTE;
    code_sum = calculate_code_sum(firmware + CODE_OFFSET, code_length);
    if (code_sum != 0x9D87u) {
        fprintf(stderr, "Internal checksum assertion failed after patch.\n");
        goto cleanup;
    }
    write_be16(firmware + CODE_CHECKSUM_OFFSET, code_sum);

    output = fopen(output_path, "wb");
    if (!output) {
        fprintf(stderr, "Cannot open output '%s': %s\n", output_path, strerror(errno));
        goto cleanup;
    }
    {
        size_t written = fwrite(firmware, 1, FLASH_SIZE, output);
        int close_error = fclose(output);
        output = NULL;
        if (written != FLASH_SIZE || close_error != 0) {
            fprintf(stderr, "Failed to write output file.\n");
            goto cleanup;
        }
    }

    printf("Patched MS2131 image written to: %s\n", output_path);
    printf("  file offset 0x%X: 0x%02X -> 0x%02X\n",
           PATCH_FILE_OFFSET, ORIGINAL_PATCH_BYTE, PATCHED_BYTE);
    printf("  code checksum: 0x%04X -> 0x%04X\n", EXPECTED_CODE_SUM, code_sum);
    printf("  input file was not modified; no device was flashed.\n");
    result = 0;

cleanup:
    if (input)
        fclose(input);
    if (output)
        fclose(output);
    free(firmware);
    return result;
}
