# Copyright (C) 2023 Intel Corporation
# SPDX-License-Identifier: AGPL-3.0

import math
import cv2
import os
import torch
import time
import hashlib
import functools
import gradio as gr
import numpy as np

import modules
import modules.paths as paths
import modules.scripts as scripts

from modules import images, devices, extra_networks, masking, shared
from modules.processing import (
    StableDiffusionProcessing, Processed, apply_overlay, apply_color_correction,
    get_fixed_seed, create_random_tensors, create_infotext, setup_color_correction
)
from modules.sd_models import CheckpointInfo
from modules.shared import Shared, opts, state

from PIL import Image, ImageOps

import openvino.frontend.pytorch.torchdynamo.backend
from openvino.frontend.pytorch.torchdynamo.execute import partitioned_modules, compiled_cache
from openvino.runtime import Core

from diffusers import (
    StableDiffusionPipeline,
    StableDiffusionImg2ImgPipeline,
    StableDiffusionInpaintPipeline,
    DDIMScheduler,
    DPMSolverMultistepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    LMSDiscreteScheduler,
    PNDMScheduler,
)

class ModelState:
    def __init__(self):
        self.recompile = 1
        self.device = "CPU"
        self.height = 512
        self.width = 512
        self.batch_size = 1
        self.mode = 0

model_state = ModelState()

def openvino_clear_caches():
    global partitioned_modules
    global compiled_cache

    compiled_cache.clear()
    partitioned_modules.clear()

def sd_diffusers_model(self):
    import modules.sd_models
    return modules.sd_models.model_data.get_sd_model()

def cond_stage_key(self):
    return None

shared.sd_diffusers_model = sd_diffusers_model

def set_scheduler(sd_model, sampler_name):
    if (sampler_name == "Euler a"):
        sd_model.scheduler = EulerAncestralDiscreteScheduler.from_config(sd_model.scheduler.config)
    elif (sampler_name == "Euler"):
        sd_model.scheduler = EulerDiscreteScheduler.from_config(sd_model.scheduler.config)
    elif (sampler_name == "LMS"):
        sd_model.scheduler = LMSDiscreteScheduler.from_config(sd_model.scheduler.config)
    elif (sampler_name == "Heun"):
        sd_model.scheduler = HeunDiscreteScheduler.from_config(sd_model.scheduler.config)
    elif (sampler_name == "DPM++ 2M"):
        sd_model.scheduler = DPMSolverMultistepScheduler.from_config(sd_model.scheduler.config, algorithm_type="dpmsolver++", use_karras_sigmas=False)
    elif (sampler_name == "LMS Karras"):
        sd_model.scheduler = LMSDiscreteScheduler.from_config(sd_model.scheduler.config, use_karras_sigmas=True)
    elif (sampler_name == "DPM++ 2M Karras"):
        sd_model.scheduler = DPMSolverMultistepScheduler.from_config(sd_model.scheduler.config, algorithm_type="dpmsolver++", use_karras_sigmas=True)
    elif (sampler_name == "DDIM"):
        sd_model.scheduler = DDIMScheduler.from_config(sd_model.scheduler.config)
    elif (sampler_name == "PLMS"):
        sd_model.scheduler = PNDMScheduler.from_config(sd_model.scheduler.config)
    else:
        sd_model.scheduler = EulerAncestralDiscreteScheduler.from_config(sd_model.scheduler.config)

    return sd_model.scheduler

def get_diffusers_sd_model(sampler_name, enable_caching, openvino_device, mode):
    if (model_state.recompile == 1):
        torch._dynamo.reset()
        openvino_clear_caches()
        curr_dir_path = os.getcwd()
        model_path = "/models/Stable-diffusion/"
        checkpoint_name = shared.opts.sd_model_checkpoint.split(" ")[0]
        checkpoint_path = curr_dir_path + model_path + checkpoint_name
        sd_model = StableDiffusionPipeline.from_single_file(checkpoint_path)
        if (mode == 1):
            sd_model = StableDiffusionImg2ImgPipeline.from_single_file(checkpoint_path)
        elif (mode == 2):
            sd_model = StableDiffusionInpaintPipeline.from_pretrained(curr_dir_path + model_path, **sd_model.components, local_files_only=True)
        checkpoint_info = CheckpointInfo(checkpoint_path)
        #model_state.mode = mode
        sd_model.sd_checkpoint_info = checkpoint_info
        sd_model.sd_model_hash = checkpoint_info.calculate_shorthash()
        sd_model.safety_checker = None
        sd_model.cond_stage_key = functools.partial(cond_stage_key, shared.sd_model)
        sd_model.scheduler = set_scheduler(sd_model, sampler_name)
        sd_model.unet = torch.compile(sd_model.unet, backend="openvino")
        sd_model.vae.decode = torch.compile(sd_model.vae.decode, backend="openvino")
        shared.sd_diffusers_model = sd_model
        del sd_model
    return shared.sd_diffusers_model


def init_new(self, all_prompts, all_seeds, all_subseeds):
    crop_region = None

    image_mask = self.image_mask

    if image_mask is not None:
        image_mask = image_mask.convert('L')

        if self.inpainting_mask_invert:
            image_mask = ImageOps.invert(image_mask)

        if self.mask_blur_x > 0:
            np_mask = np.array(image_mask)
            kernel_size = 2 * int(4 * self.mask_blur_x + 0.5) + 1
            np_mask = cv2.GaussianBlur(np_mask, (kernel_size, 1), self.mask_blur_x)
            image_mask = Image.fromarray(np_mask)

        if self.mask_blur_y > 0:
            np_mask = np.array(image_mask)
            kernel_size = 2 * int(4 * self.mask_blur_y + 0.5) + 1
            np_mask = cv2.GaussianBlur(np_mask, (1, kernel_size), self.mask_blur_y)
            image_mask = Image.fromarray(np_mask)

        if self.inpaint_full_res:
            self.mask_for_overlay = image_mask
            mask = image_mask.convert('L')
            crop_region = masking.get_crop_region(np.array(mask), self.inpaint_full_res_padding)
            crop_region = masking.expand_crop_region(crop_region, self.width, self.height, mask.width, mask.height)
            x1, y1, x2, y2 = crop_region

            mask = mask.crop(crop_region)
            image_mask = images.resize_image(2, mask, self.width, self.height)
            self.paste_to = (x1, y1, x2-x1, y2-y1)
        else:
            image_mask = images.resize_image(self.resize_mode, image_mask, self.width, self.height)
            np_mask = np.array(image_mask)
            np_mask = np.clip((np_mask.astype(np.float32)) * 2, 0, 255).astype(np.uint8)
            self.mask_for_overlay = Image.fromarray(np_mask)

        self.overlay_images = []

    latent_mask = self.latent_mask if self.latent_mask is not None else image_mask

    add_color_corrections = opts.img2img_color_correction and self.color_corrections is None
    if add_color_corrections:
        self.color_corrections = []
    imgs = []
    for img in self.init_images:
        # Save init image
        if opts.save_init_img:
            self.init_img_hash = hashlib.md5(img.tobytes()).hexdigest()
            images.save_image(img, path=opts.outdir_init_images, basename=None, forced_filename=self.init_img_hash, save_to_dirs=False)

        image = images.flatten(img, opts.img2img_background_color)

        if crop_region is None and self.resize_mode != 3:
            image = images.resize_image(self.resize_mode, image, self.width, self.height)

        if image_mask is not None:
            image_masked = Image.new('RGBa', (image.width, image.height))
            image_masked.paste(image.convert("RGBA").convert("RGBa"), mask=ImageOps.invert(self.mask_for_overlay.convert('L')))
            self.mask = image_mask
            image_mask.save("/home/yamini/pytorch_fx/test/mask_image.jpg")
            self.overlay_images.append(image_masked.convert('RGBA'))

        # crop_region is not None if we are doing inpaint full res
        if crop_region is not None:
            image = image.crop(crop_region)
            image = images.resize_image(2, image, self.width, self.height)

        self.init_images = image
        if image_mask is not None:
            if self.inpainting_fill != 1:
                image = masking.fill(image, latent_mask)

        if add_color_corrections:
            self.color_corrections.append(setup_color_correction(image))

        image = np.array(image).astype(np.float32) / 255.0
        image = np.moveaxis(image, 2, 0)

        imgs.append(image)

    if len(imgs) == 1:
        batch_images = np.expand_dims(imgs[0], axis=0).repeat(self.batch_size, axis=0)
        if self.overlay_images is not None:
            self.overlay_images = self.overlay_images * self.batch_size

        if self.color_corrections is not None and len(self.color_corrections) == 1:
            self.color_corrections = self.color_corrections * self.batch_size

    elif len(imgs) <= self.batch_size:
        self.batch_size = len(imgs)
        batch_images = np.array(imgs)
    else:
        raise RuntimeError(f"bad number of images passed: {len(imgs)}; expecting {self.batch_size} or less")


def process_images_openvino(p: StableDiffusionProcessing, sampler_name, enable_caching, openvino_device, mode) -> Processed:
    """this is the main loop that both txt2img and img2img use; it calls func_init once inside all the scopes and func_sample once per batch"""

    if type(p.prompt) == list:
        assert(len(p.prompt) > 0)
    else:
        assert p.prompt is not None

    devices.torch_gc()

    seed = get_fixed_seed(p.seed)
    subseed = get_fixed_seed(p.subseed)

    comments = {}

    p.setup_prompts()

    if type(seed) == list:
        p.all_seeds = seed
    else:
        p.all_seeds = [int(seed) + (x if p.subseed_strength == 0 else 0) for x in range(len(p.all_prompts))]

    if type(subseed) == list:
        p.all_subseeds = subseed
    else:
        p.all_subseeds = [int(subseed) + x for x in range(len(p.all_prompts))]

    def infotext(iteration=0, position_in_batch=0):
        return create_infotext(p, p.all_prompts, p.all_seeds, p.all_subseeds, comments, iteration, position_in_batch)

    if p.scripts is not None:
        p.scripts.process(p)

    infotexts = []
    output_images = []

    with torch.no_grad():
        with devices.autocast():
            p.init(p.all_prompts, p.all_seeds, p.all_subseeds)

        if state.job_count == -1:
            state.job_count = p.n_iter

        extra_network_data = None
        for n in range(p.n_iter):
            p.iteration = n

            if state.skipped:
                state.skipped = False

            if state.interrupted:
                break

            p.prompts = p.all_prompts[n * p.batch_size:(n + 1) * p.batch_size]
            p.negative_prompts = p.all_negative_prompts[n * p.batch_size:(n + 1) * p.batch_size]
            p.seeds = p.all_seeds[n * p.batch_size:(n + 1) * p.batch_size]
            p.subseeds = p.all_subseeds[n * p.batch_size:(n + 1) * p.batch_size]

            if p.scripts is not None:
                p.scripts.before_process_batch(p, batch_number=n, prompts=p.prompts, seeds=p.seeds, subseeds=p.subseeds)

            if len(p.prompts) == 0:
                break

            if (model_state.height != p.height or model_state.width != p.width or model_state.batch_size != p.batch_size or model_state.mode != mode):
                model_state.recompile = 1
                model_state.height = p.height
                model_state.width = p.width
                model_state.batch_size = p.batch_size
                model_state.mode = mode

            shared.sd_diffusers_model = get_diffusers_sd_model(sampler_name, enable_caching, openvino_device, mode)
            shared.sd_diffusers_model.scheduler = set_scheduler(shared.sd_diffusers_model, sampler_name)

            extra_network_data = p.parse_extra_network_prompts()

            if not p.disable_extra_networks:
                with devices.autocast():
                    extra_networks.activate(p, p.extra_network_data)

            # TODO: support multiplier
            if ('lora' in modules.extra_networks.extra_network_registry):
                import lora
                for lora_model in lora.loaded_loras:
                    shared.sd_diffusers_model.load_lora_weights(os.getcwd() + "/models/Lora/", weight_name=lora_model.name + ".safetensors")

            if p.scripts is not None:
                p.scripts.process_batch(p, batch_number=n, prompts=p.prompts, seeds=p.seeds, subseeds=p.subseeds)

            # params.txt should be saved after scripts.process_batch, since the
            # infotext could be modified by that callback
            # Example: a wildcard processed by process_batch sets an extra model
            # strength, which is saved as "Model Strength: 1.0" in the infotext
            if n == 0:
                with open(os.path.join(paths.data_path, "params.txt"), "w", encoding="utf8") as file:
                    processed = Processed(p, [], p.seed, "")
                    file.write(create_infotext(p, p.all_prompts, p.all_seeds, p.all_subseeds, comments=[], position_in_batch=0 % p.batch_size, iteration=0 // p.batch_size))

            if p.n_iter > 1:
                shared.state.job = f"Batch {n+1} out of {p.n_iter}"

            generator = [torch.Generator(device="cpu").manual_seed(s) for s in p.seeds]

            time_stamps = []

            def callback(iter, t, latents):
                time_stamps.append(time.time())

            time_stamps.append(time.time())
            if (mode == 0):
                output = shared.sd_diffusers_model(
                    prompt=p.prompts,
                    negative_prompt=p.negative_prompts,
                    num_inference_steps=p.steps,
                    guidance_scale=p.cfg_scale,
                    width = p.width,
                    height = p.height,
                    generator=generator,
                    output_type="np",
                    callback = callback,
                    callback_steps = 1,
                )
            elif (mode == 1):
                output = shared.sd_diffusers_model(
                    prompt=p.prompts,
                    negative_prompt=p.negative_prompts,
                    num_inference_steps=p.steps,
                    guidance_scale=p.cfg_scale,
                    image = p.init_images,
                    strength = p.denoising_strength,
                    generator=generator,
                    output_type="np",
                    callback = callback,
                    callback_steps = 1,
                )
            else:
                output = shared.sd_diffusers_model(
                    prompt=p.prompts,
                    negative_prompt=p.negative_prompts,
                    num_inference_steps=p.steps,
                    guidance_scale=p.cfg_scale,
                    mask_image = p.mask,
                    image = p.init_images,
                    strength = p.denoising_strength,
                    generator=generator,
                    output_type="np",
                    callback = callback,
                    callback_steps = 1,
                )
            
            model_state.recompile = 0

            warmup_duration = time_stamps[1] - time_stamps[0]
            generation_rate = (p.steps - 1) / (time_stamps[-1] - time_stamps[1])

            x_samples_ddim = output.images

            for i, x_sample in enumerate(x_samples_ddim):
                p.batch_index = i

                x_sample = (255. * x_sample).astype(np.uint8)

                if p.restore_faces:
                    if opts.save and not p.do_not_save_samples and opts.save_images_before_face_restoration:
                        images.save_image(Image.fromarray(x_sample), p.outpath_samples, "", p.seeds[i], p.prompts[i], opts.samples_format, info=infotext(n, i), p=p, suffix="-before-face-restoration")

                    devices.torch_gc()

                    x_sample = modules.face_restoration.restore_faces(x_sample)
                    devices.torch_gc()

                image = Image.fromarray(x_sample)

                if p.scripts is not None:
                    pp = scripts.PostprocessImageArgs(image)
                    p.scripts.postprocess_image(p, pp)
                    image = pp.image

                if p.color_corrections is not None and i < len(p.color_corrections):
                    if opts.save and not p.do_not_save_samples and opts.save_images_before_color_correction:
                        image_without_cc = apply_overlay(image, p.paste_to, i, p.overlay_images)
                        images.save_image(image_without_cc, p.outpath_samples, "", p.seeds[i], p.prompts[i], opts.samples_format, info=infotext(n, i), p=p, suffix="-before-color-correction")
                    image = apply_color_correction(p.color_corrections[i], image)

                image = apply_overlay(image, p.paste_to, i, p.overlay_images)

                if opts.samples_save and not p.do_not_save_samples:
                    images.save_image(image, p.outpath_samples, "", p.seeds[i], p.prompts[i], opts.samples_format, info=infotext(n, i), p=p)

                text = infotext(n, i)
                infotexts.append(text)
                if opts.enable_pnginfo:
                    image.info["parameters"] = text
                output_images.append(image)

                if hasattr(p, 'mask_for_overlay') and p.mask_for_overlay and any([opts.save_mask, opts.save_mask_composite, opts.return_mask, opts.return_mask_composite]):
                    image_mask = p.mask_for_overlay.convert('RGB')
                    image_mask_composite = Image.composite(image.convert('RGBA').convert('RGBa'), Image.new('RGBa', image.size), images.resize_image(2, p.mask_for_overlay, image.width, image.height).convert('L')).convert('RGBA')

                    if opts.save_mask:
                        images.save_image(image_mask, p.outpath_samples, "", p.seeds[i], p.prompts[i], opts.samples_format, info=infotext(n, i), p=p, suffix="-mask")

                    if opts.save_mask_composite:
                        images.save_image(image_mask_composite, p.outpath_samples, "", p.seeds[i], p.prompts[i], opts.samples_format, info=infotext(n, i), p=p, suffix="-mask-composite")

                    if opts.return_mask:
                        output_images.append(image_mask)

                    if opts.return_mask_composite:
                        output_images.append(image_mask_composite)

            del x_samples_ddim

            devices.torch_gc()

            state.nextjob()

        p.color_corrections = None

        index_of_first_image = 0
        unwanted_grid_because_of_img_count = len(output_images) < 2 and opts.grid_only_if_multiple
        if (opts.return_grid or opts.grid_save) and not p.do_not_save_grid and not unwanted_grid_because_of_img_count:
            grid = images.image_grid(output_images, p.batch_size)

            if opts.return_grid:
                text = infotext()
                infotexts.insert(0, text)
                if opts.enable_pnginfo:
                    grid.info["parameters"] = text
                output_images.insert(0, grid)
                index_of_first_image = 1

            if opts.grid_save:
                images.save_image(grid, p.outpath_grids, "grid", p.all_seeds[0], p.all_prompts[0], opts.grid_format, info=infotext(), short_filename=not opts.grid_extended_filename, p=p, grid=True)

    if not p.disable_extra_networks and extra_network_data:
        extra_networks.deactivate(p, p.extra_network_data)

    devices.torch_gc()

    res = Processed(
        p,
        images_list=output_images,
        seed=p.all_seeds[0],
        info=infotext(),
        comments="".join(f"{comment}\n" for comment in comments),
        subseed=p.all_subseeds[0],
        index_of_first_image=index_of_first_image,
        infotexts=infotexts,
    )

    res.info = res.info + ", Warm up time: " + str(round(warmup_duration, 2)) + " secs "

    if (generation_rate >= 1.0):
        res.info = res.info + ", Performance: " + str(round(generation_rate, 2)) + " it/s "
    else:
        res.info = res.info + ", Performance: " + str(round(1/generation_rate, 2)) + " s/it "


    if p.scripts is not None:
        p.scripts.postprocess(p, res)

    return res

class Script(scripts.Script):
    def title(self):
        return "Accelerate with OpenVINO"

    def show(self, is_img2img):
        return True

    def ui(self, is_img2img):
        core = Core()
        openvino_device = gr.Dropdown(label="Select a device", choices=[device for device in core.available_devices], value=model_state.device)
        override_sampler = gr.Checkbox(label="Override the sampling selection from the main UI (Recommended as only below sampling methods have been validated for OpenVINO)", value=True)
        sampler_name = gr.Radio(label="Select a sampling method", choices=["Euler a", "Euler", "LMS", "Heun", "DPM++ 2M", "LMS Karras", "DPM++ 2M Karras", "DDIM", "PLMS"], value="Euler a")
        enable_caching = gr.Checkbox(label="Cache the compiled models on disk for faster model load in subsequent launches (Recommended)", value=True, elem_id=self.elem_id("enable_caching"))
        warmup_status = gr.Textbox(label="Device", interactive=False, visible=False)
        warmup_note = gr.Markdown(
                      """
                      ###
                      ### Note:
                      First inference involves compilation of the model for best performance.
                      Excluding the first inference (or warm up inference) is recommended for
                      performance measurements. When resolution, batchsize, or device is changed,
                      or samplers like DPM++ or Karras are selected, model is recompiled. Subsequent
                      iterations use the cached compiled model for faster inference.
                      """)

        def device_change(choice):
            if (model_state.device == choice):
                return gr.update(value="Device selected is " + choice, visible=True)
            else:
                model_state.device = choice
                model_state.recompile = 1
                return gr.update(value="Device changed to " + choice + ". Model will be re-compiled", visible=True)
        openvino_device.change(device_change, openvino_device, warmup_status)

        return [openvino_device, override_sampler, sampler_name, enable_caching]

    def run(self, p, openvino_device, override_sampler, sampler_name, enable_caching):
        os.environ["OPENVINO_TORCH_BACKEND_DEVICE"] = str(openvino_device)
        if enable_caching:
            os.environ["OPENVINO_TORCH_MODEL_CACHING"] = "1"

        if override_sampler:
            p.sampler_name = sampler_name
        else:
            supported_samplers = ["Euler a", "Euler", "LMS", "Heun", "DPM++ 2M", "LMS Karras", "DPM++ 2M Karras", "DDIM", "PLMS"]
            if (p.sampler_name not in supported_samplers):
                p.sampler_name = "Euler a"

        # mode can be 0, 1, 2 corresponding to txt2img, img2img, inpaint respectively
        mode = 0
        if self.is_txt2img:
            mode = 0
            processed = process_images_openvino(p, p.sampler_name, enable_caching, openvino_device, mode)
        else:
            if p.image_mask is None:
                mode = 1 
            else:
                mode = 2
            p.init = functools.partial(init_new, p)
            processed = process_images_openvino(p, p.sampler_name, enable_caching, openvino_device, mode)
        return processed


