import threading
from queue import Queue
from typing import List, Tuple, Generator

import torch
import numpy as np
from PIL import Image

from surya.model.detection.model import EfficientViTForSemanticSegmentation
from surya.postprocessing.heatmap import get_and_clean_boxes
from surya.postprocessing.affinity import get_vertical_lines
from surya.input.processing import prepare_image_detection, split_image, get_total_splits, convert_if_not_rgb
from surya.schema import TextDetectionResult
from surya.settings import settings
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor
import torch.nn.functional as F


def get_batch_size():
    batch_size = settings.DETECTOR_BATCH_SIZE
    if batch_size is None:
        batch_size = 8
        if settings.TORCH_DEVICE_MODEL == "mps":
            batch_size = 8
        if settings.TORCH_DEVICE_MODEL == "cuda":
            batch_size = 36
    return batch_size


def batch_detection(
    images: List,
    model: EfficientViTForSemanticSegmentation,
    processor,
    batch_size=None
) -> Generator[Tuple[List[List[np.ndarray]], List[Tuple[int, int]]], None, None]:
    assert all([isinstance(image, Image.Image) for image in images])
    if batch_size is None:
        batch_size = get_batch_size()
    heatmap_count = model.config.num_labels

    orig_sizes = [image.size for image in images]
    splits_per_image = [get_total_splits(size, processor) for size in orig_sizes]

    batches = []
    current_batch_size = 0
    current_batch = []
    for i in range(len(images)):
        if current_batch_size + splits_per_image[i] > batch_size:
            if len(current_batch) > 0:
                batches.append(current_batch)
            current_batch = []
            current_batch_size = 0
        current_batch.append(i)
        current_batch_size += splits_per_image[i]

    if len(current_batch) > 0:
        batches.append(current_batch)

    for batch_idx in tqdm(range(len(batches)), desc="Detecting bboxes"):
        batch_image_idxs = batches[batch_idx]
        batch_images = [images[j].convert("RGB") for j in batch_image_idxs]

        split_index = []
        split_heights = []
        image_splits = []
        for image_idx, image in enumerate(batch_images):
            image_parts, split_height = split_image(image, processor)
            image_splits.extend(image_parts)
            split_index.extend([image_idx] * len(image_parts))
            split_heights.extend(split_height)

        image_splits = [prepare_image_detection(image, processor) for image in image_splits]
        # Batch images in dim 0
        batch = torch.stack(image_splits, dim=0).to(model.dtype).to(model.device)

        with torch.inference_mode():
            pred = model(pixel_values=batch)

        logits = pred.logits
        correct_shape = [processor.size["height"], processor.size["width"]]
        current_shape = list(logits.shape[2:])
        if current_shape != correct_shape:
            logits = F.interpolate(logits, size=correct_shape, mode='bilinear', align_corners=False)

        logits = logits.cpu().detach().numpy().astype(np.float32)
        preds = []
        for i, (idx, height) in enumerate(zip(split_index, split_heights)):
            # If our current prediction length is below the image idx, that means we have a new image
            # Otherwise, we need to add to the current image
            if len(preds) <= idx:
                preds.append([logits[i][k] for k in range(heatmap_count)])
            else:
                heatmaps = preds[idx]
                pred_heatmaps = [logits[i][k] for k in range(heatmap_count)]

                if height < processor.size["height"]:
                    # Cut off padding to get original height
                    pred_heatmaps = [pred_heatmap[:height, :] for pred_heatmap in pred_heatmaps]

                for k in range(heatmap_count):
                    heatmaps[k] = np.vstack([heatmaps[k], pred_heatmaps[k]])
                preds[idx] = heatmaps

        yield preds, [orig_sizes[j] for j in batch_image_idxs]


def parallel_get_lines(preds, orig_sizes):
    heatmap, affinity_map = preds
    heat_img = Image.fromarray((heatmap * 255).astype(np.uint8))
    aff_img = Image.fromarray((affinity_map * 255).astype(np.uint8))
    affinity_size = list(reversed(affinity_map.shape))
    heatmap_size = list(reversed(heatmap.shape))
    bboxes = get_and_clean_boxes(heatmap, heatmap_size, orig_sizes)
    vertical_lines = get_vertical_lines(affinity_map, affinity_size, orig_sizes)

    result = TextDetectionResult(
        bboxes=bboxes,
        vertical_lines=vertical_lines,
        heatmap=heat_img,
        affinity_map=aff_img,
        image_bbox=[0, 0, orig_sizes[0], orig_sizes[1]]
    )
    return result


def batch_text_detection(images: List, model, processor, batch_size=None) -> List[TextDetectionResult]:
    detection_generator = batch_detection(images, model, processor, batch_size=batch_size)

    results = []
    result_lock = threading.Lock()
    max_workers = min(settings.DETECTOR_POSTPROCESSING_CPU_WORKERS, len(images))
    parallelize = not settings.IN_STREAMLIT and len(images) >= settings.DETECTOR_MIN_PARALLEL_THRESH
    batch_queue = Queue(maxsize=4)

    def inference_producer():
        for batch in detection_generator:
            batch_queue.put(batch)
        batch_queue.put(None)  # Signal end of batches

    def postprocessing_consumer():
        if parallelize:
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                while True:
                    batch = batch_queue.get()
                    if batch is None:
                        break

                    preds, orig_sizes = batch
                    batch_results = list(executor.map(parallel_get_lines, preds, orig_sizes))

                    with result_lock:
                        results.extend(batch_results)
        else:
            while True:
                batch = batch_queue.get()
                if batch is None:
                    break

                preds, orig_sizes = batch
                batch_results = [parallel_get_lines(pred, orig_size)
                                 for pred, orig_size in zip(preds, orig_sizes)]

                with result_lock:
                    results.extend(batch_results)

    # Start producer and consumer threads
    producer = threading.Thread(target=inference_producer)
    consumer = threading.Thread(target=postprocessing_consumer)

    producer.start()
    consumer.start()

    # Wait for both threads to complete
    producer.join()
    consumer.join()

    return results


