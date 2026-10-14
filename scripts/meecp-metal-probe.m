#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>

// Fixed synthetic compute only. No selected code, device names, or diagnostics.
static int report(bool device, bool residency, bool compute) {
    printf("{\"schema_version\":1,\"device_available\":%s,\"residency_available\":%s,\"compute_passed\":%s}\n",
           device ? "true" : "false", residency ? "true" : "false", compute ? "true" : "false");
    return compute ? 0 : 1;
}

int main(int argc, const char *argv[]) {
    (void)argv;
    if (argc != 1) return 2;
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (!device) return report(false, false, false);
        NSError *error = nil;
        MTLResidencySetDescriptor *descriptor = [[MTLResidencySetDescriptor alloc] init];
        descriptor.initialCapacity = 1;
        id<MTLResidencySet> residency = [device newResidencySetWithDescriptor:descriptor error:&error];
        if (!residency) return report(true, false, false);
        id<MTLBuffer> buffer = [device newBufferWithLength:sizeof(float) * 4 options:MTLResourceStorageModeShared];
        if (!buffer) return report(true, true, false);
        [residency addAllocation:buffer];
        [residency commit];
        id<MTLLibrary> library = [device newLibraryWithSource:
            @"#include <metal_stdlib>\nusing namespace metal;\nkernel void twice(device float* values [[buffer(0)]], uint i [[thread_position_in_grid]]) { values[i] *= 2.0f; }"
            options:nil error:&error];
        if (!library) return report(true, true, false);
        id<MTLComputePipelineState> pipeline = [device newComputePipelineStateWithFunction:
            [library newFunctionWithName:@"twice"] error:&error];
        if (!pipeline) return report(true, true, false);
        id<MTLCommandQueue> queue = [device newCommandQueue];
        if (!queue) return report(true, true, false);
        [queue addResidencySet:residency];
        float *values = buffer.contents;
        for (int i = 0; i < 4; i++) values[i] = (float)(i + 1);
        id<MTLCommandBuffer> command = [queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [command computeCommandEncoder];
        if (!command || !encoder) return report(true, true, false);
        [encoder setComputePipelineState:pipeline];
        [encoder setBuffer:buffer offset:0 atIndex:0];
        [encoder dispatchThreads:MTLSizeMake(4, 1, 1) threadsPerThreadgroup:MTLSizeMake(4, 1, 1)];
        [encoder endEncoding];
        [command commit];
        [command waitUntilCompleted];
        if (command.status != MTLCommandBufferStatusCompleted) return report(true, true, false);
        for (int i = 0; i < 4; i++) if (values[i] != (float)(2 * (i + 1))) return report(true, true, false);
        return report(true, true, true);
    }
}
