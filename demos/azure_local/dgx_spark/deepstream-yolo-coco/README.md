# DeepStream YOLO COCO Setup

This directory sets up a COCO-pretrained YOLO11 model for DeepStream 8.0 on ARM SBSA.

The DeepStream config keeps the full 80-class COCO model, but filters output to:

- `0` person
- `14` bird
- `15` cat
- `16` dog
- `17` horse
- `18` sheep
- `19` cow
- `20` elephant
- `21` bear
- `22` zebra
- `23` giraffe

## Build

```bash
cd /home/anslutsky/Dev/Cosmos-transfer/deepstream-yolo-coco
chmod +x *.sh
./build_runtime_image.sh
./prepare_yolo11_model.sh
./build_parser.sh
```

Defaults:

- Runtime image: `deepstream-yolo-coco:8.0-samples-sbsa`
- Base DeepStream image: `nvcr.io/nvidia/deepstream:8.0-samples-multiarch`
- Model: `yolo11s`
- Input size: `640`
- ONNX opset: `18`
- TensorRT mode: FP16

On DGX Spark/SBSA, `Dockerfile.runtime` adds GStreamer libav decoders and reuses
the local VSS SBSA image's versioned DeepStream runtime libraries.

For the fastest variant, export the nano model instead:

```bash
YOLO_MODEL=yolo11n ./prepare_yolo11_model.sh
```

## Run

Headless smoke test with NVIDIA's sample clip:

```bash
./run_deepstream_yolo.sh headless
```

Display mode:

```bash
./run_deepstream_yolo.sh display
```

Custom source:

```bash
./run_deepstream_yolo.sh headless file:///absolute/path/to/video.mp4
./run_deepstream_yolo.sh headless rtsp://camera/stream
```

The first DeepStream run builds `DeepStream-Yolo/model_b1_gpu0_fp16.engine`, which can take several minutes.
