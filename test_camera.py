from diffusers import AutoPipelineForText2Image
import torch
import os

print("Loading SDXL-Turbo into Unified Memory...")
# We load the fp16 (half-precision) variant to keep it lightning fast
pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/sdxl-turbo", 
    torch_dtype=torch.float16, 
    variant="fp16"
)

# 🚀 Move the pipeline to Apple's Metal Performance Shaders (GPU)
pipe.to("mps")

# The prompt Skippy would theoretically write
prompt = "Macro photography, 8k resolution, a beautifully machined titanium spur gear resting on a dark wooden workshop bench, dramatic lighting"
print(f"\nGenerating Image: '{prompt}'")

# SDXL-Turbo is so fast it only requires 2 steps to create a photorealistic image
image = pipe(prompt=prompt, num_inference_steps=2, guidance_scale=0.0).images[0]

# Save it directly to your Mac desktop
desktop_path = os.path.expanduser("~/Desktop/skippy_test_gear.png")
image.save(desktop_path)

print(f"\n✅ Done! Check your desktop for: {desktop_path}")
