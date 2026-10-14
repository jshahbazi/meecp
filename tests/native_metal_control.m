#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>
#include <string.h>

// Independent synthetic hardware admission, never a production helper. Only
// absent device (75), unsupported feature family (76), or Metal's explicit
// unsupported residency error (77) skips. Unclassified failures remain fatal.
static int residencyFailureStatus(NSError *error) {
#if __MAC_OS_X_VERSION_MAX_ALLOWED >= 260400
    if (@available(macOS 26.4, *)) {
        if ([error.domain isEqualToString:MTLDeviceErrorDomain] &&
            error.code == MTLDeviceErrorNotSupported) return 77;
    }
#endif
    return 11;
}

int main(void) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) return 75;
        // Apple's feature table lists residency sets for Apple6 and later or
        // Mac2: https://developer.apple.com/metal/capabilities/
        // Do not infer support from a device's name or CI flag.
        if (![device supportsFamily:MTLGPUFamilyApple6] &&
            ![device supportsFamily:MTLGPUFamilyMac2]) return 76;
        NSError *error = nil;
        MTLResidencySetDescriptor *descriptor = [MTLResidencySetDescriptor new];
        descriptor.initialCapacity = 1;
        id<MTLResidencySet> residency = [device newResidencySetWithDescriptor:descriptor error:&error];
        // A feature family does not guarantee that the current device/driver
        // implements residency sets. Classify only Metal's documented error;
        // never emit the error description or skip an unknown allocation failure.
        if (!residency) {
            int status = residencyFailureStatus(error);
            if (status == 11) {
                // Only the bounded framework error identifier/code is reported.
                // Error descriptions and userInfo may contain private data.
                const char *domain = error ? error.domain.UTF8String : "none";
                if (!domain || !*domain || strlen(domain) > 128 ||
                    strspn(domain, "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.") != strlen(domain))
                    domain = "unrecognized";
                NSInteger code = error.code;
                if (code < -65535 || code > 65535) code = 65535;
                printf("residency_error:%s:%ld\n", domain, (long)code);
            }
            return status;
        }
        id<MTLBuffer> buffer = [device newBufferWithLength:sizeof(uint32_t) * 4
            options:MTLResourceStorageModeShared];
        if (!buffer || !buffer.contents) return 12;
        [residency addAllocation:buffer];
        [residency commit];
        id<MTLLibrary> library = [device newLibraryWithSource:
            @"#include <metal_stdlib>\nusing namespace metal;\nkernel void add_seven(device uint* values [[buffer(0)]], uint i [[thread_position_in_grid]]) { values[i] += 7u; }"
            options:nil error:&error];
        if (!library) return 13;
        id<MTLFunction> function = [library newFunctionWithName:@"add_seven"];
        if (!function) return 14;
        id<MTLComputePipelineState> pipeline = [device newComputePipelineStateWithFunction:function error:&error];
        if (!pipeline) return 15;
        id<MTLCommandQueue> queue = [device newCommandQueue];
        if (!queue) return 16;
        [queue addResidencySet:residency];
        uint32_t *values = buffer.contents;
        for (uint32_t i = 0; i < 4; i++) values[i] = i * 3;
        id<MTLCommandBuffer> command = [queue commandBuffer];
        if (!command) return 17;
        id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
        if (!encoder) return 18;
        [encoder setComputePipelineState:pipeline];
        [encoder setBuffer:buffer offset:0 atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(4, 1, 1) threadsPerThreadgroup:MTLSizeMake(4, 1, 1)];
        [encoder endEncoding];
        [command commit];
        [command waitUntilCompleted];
        if (command.status != MTLCommandBufferStatusCompleted) return 19;
        for (uint32_t i = 0; i < 4; i++) if (values[i] != i * 3 + 7) return 20;
        return 0;
    }
}
