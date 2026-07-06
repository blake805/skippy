import gradio as gr
import torch
from diffusers import AutoPipelineForText2Image

print("Loading RealVisXL Image Engine for Web UI...")
# Load the exact same uncensored model onto the M3 Ultra GPU
image_pipe = AutoPipelineForText2Image.from_pretrained(
    "SG161222/RealVisXL_V4.0_Lightning", 
    torch_dtype=torch.float16, 
    variant="fp16"
)
image_pipe.to("mps")

def generate_image(prompt):
    """The function that runs when you press Submit on your phone."""
    print(f"\n[Web Request] Drawing: {prompt}")
    image = image_pipe(
        prompt=prompt, 
        num_inference_steps=6, 
        guidance_scale=1.5
    ).images[0]
    return image

# Build the slick Web UI
with gr.Blocks(theme=gr.themes.Monochrome()) as dashboard:
    gr.Markdown("# 🚀 Skippy's Visual Cortex")
    gr.Markdown("Generate uncensored, local images directly on the Mac Studio M3 Ultra.")
    
    with gr.Row():
        with gr.Column(scale=1):
            text_input = gr.Textbox(label="Image Prompt", lines=3, placeholder="Describe the image...")
            submit_btn = gr.Button("Generate", variant="primary")
        
        with gr.Column(scale=2):
            image_output = gr.Image(label="Output")
            
    # Connect the button to the function
    submit_btn.click(fn=generate_image, inputs=text_input, outputs=image_output)

print("\n==============================================")
print(" 🌐 WEB DASHBOARD LIVE ON LOCAL NETWORK ")
print("==============================================")

# Launch the server (0.0.0.0 exposes it to your local Wi-Fi)
dashboard.launch(server_name="0.0.0.0", server_port=7860)
