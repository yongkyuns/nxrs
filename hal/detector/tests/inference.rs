#![cfg(feature = "person-model")]
use detector::{Detector, Image, MobileNet};
use microflow::{
    buffer::Buffer2D,
    tensor::{Tensor4D, TensorView, TensorViewPadding},
};

#[test]
fn tensorflow_same_stride_two_even_input_pads_bottom_and_right() {
    let input: Tensor4D<i8, 1, 4, 4, 1, 1> = Tensor4D::new(
        [Buffer2D::from_fn(|r, c| [(r * 4 + c + 1) as i8])],
        [1.0],
        [0],
    );
    let first: TensorView<i8, 3, 3, 1> = input.view((0, 0), 0, TensorViewPadding::Same, (2, 2));
    assert_eq!(first.buffer[(0, 0)], [1]);
    assert_eq!(first.buffer[(2, 2)], [11]);
    assert_eq!(first.len, 9);
    let last: TensorView<i8, 3, 3, 1> = input.view((1, 1), 0, TensorViewPadding::Same, (2, 2));
    assert_eq!(last.buffer[(0, 0)], [11]);
    assert_eq!(last.buffer[(1, 1)], [16]);
    assert!(!last.mask[(2, 1)] && !last.mask[(1, 2)]);
    assert_eq!(last.len, 4);
}

#[test]
fn same_padding_handles_each_dimension_independently() {
    let input: Tensor4D<i8, 1, 4, 5, 1, 1> = Tensor4D::new(
        [Buffer2D::from_fn(|r, c| [(r * 5 + c + 1) as i8])],
        [1.0],
        [0],
    );
    let first: TensorView<i8, 3, 3, 1> = input.view((0, 0), 0, TensorViewPadding::Same, (2, 2));
    assert!(!first.mask[(0, 0)]);
    assert_eq!(first.buffer[(0, 1)], [1]);
    assert_eq!(first.buffer[(2, 2)], [12]);
    assert_eq!(first.len, 6);
    let stride_one: TensorView<i8, 3, 3, 1> =
        input.view((0, 0), 0, TensorViewPadding::Same, (1, 1));
    assert_eq!(stride_one.buffer[(1, 1)], [1]);
    assert_eq!(stride_one.len, 4);
}

#[test]
fn actual_model_matches_independent_tensorflow_lite_smoke_scores() {
    // TFLite 2.20.0, one CPU thread, input bytes minus 128. Integer TFLite
    // requantization and MicroFlow's float constants need not be bit-identical.
    let mut model = MobileNet::default();
    let person = model
        .rgb(include_bytes!(
            "../../../tests/nuttx-std/detection/person.rgb"
        ))
        .unwrap()
        .permille();
    let background = model
        .rgb(include_bytes!(
            "../../../tests/nuttx-std/detection/background.rgb"
        ))
        .unwrap()
        .permille();
    assert!(person.abs_diff(863) <= 16, "person score {person}");
    assert!(
        background.abs_diff(105) <= 16,
        "background score {background}"
    );
}

#[test]
fn actual_jpeg_path_detects_person_and_rejects_bad_headers() {
    let mut model = MobileNet::default();
    let person = include_bytes!("../../../tests/nuttx-std/detection/person.jpg");
    let background = include_bytes!("../../../tests/nuttx-std/detection/background.jpg");
    assert!(
        model
            .detect(Image::jpeg(person, 96, 96))
            .unwrap()
            .person_score
            .permille()
            >= 500
    );
    assert!(
        model
            .detect(Image::jpeg(background, 96, 96))
            .unwrap()
            .person_score
            .permille()
            < 500
    );
    assert!(model.detect(Image::jpeg(person, 95, 96)).is_err());
    let mut progressive = person.to_vec();
    let sof = progressive
        .windows(2)
        .position(|v| v == [255, 192])
        .unwrap();
    progressive[sof + 1] = 194;
    assert!(model.detect(Image::jpeg(&progressive, 96, 96)).is_err());
    let mut oversized = person.to_vec();
    oversized.resize(153601, 0);
    assert!(model.detect(Image::jpeg(&oversized, 96, 96)).is_err());
}

#[test]
fn pointwise_convolution_preserves_asymmetric_quantization_and_strides() {
    use microflow::activation::FusedActivation;
    use microflow::ops::{conv_2d, Conv2DOptions};
    let input: Tensor4D<i8, 1, 2, 2, 2, 1> = Tensor4D::new(
        [Buffer2D::from_fn(|r, c| {
            let v = (r * 2 + c) as i8 * 4;
            [v + 4, v + 6]
        })],
        [1.0],
        [3],
    );
    let weights: Tensor4D<i8, 2, 1, 1, 2, 2> = Tensor4D::new(
        [
            Buffer2D::from_element([2, 1]),
            Buffer2D::from_element([0, 1]),
        ],
        [1.0, 1.0],
        [1, -2],
    );
    let options = Conv2DOptions {
        fused_activation: FusedActivation::None,
        view_padding: TensorViewPadding::Same,
        strides: (1, 1),
    };
    let output: Tensor4D<i8, 1, 2, 2, 2, 1> = conv_2d(
        input,
        &weights,
        [1.0],
        [0],
        options,
        (Buffer2D::from_element(0.0), Buffer2D::from_element(1.0)),
    );
    // Hand-computed dot products after subtracting each tensor's zero point.
    assert_eq!(output.buffer[0][(0, 0)], [1, 11]);
    assert_eq!(output.buffer[0][(0, 1)], [5, 31]);
    assert_eq!(output.buffer[0][(1, 0)], [9, 51]);
    assert_eq!(output.buffer[0][(1, 1)], [13, 71]);
    let input: Tensor4D<i8, 1, 2, 2, 2, 1> = Tensor4D::new(
        [Buffer2D::from_fn(|r, c| {
            let v = (r * 2 + c) as i8 * 4;
            [v + 4, v + 6]
        })],
        [1.0],
        [3],
    );
    let options = Conv2DOptions {
        fused_activation: FusedActivation::Relu6,
        view_padding: TensorViewPadding::Valid,
        strides: (1, 2),
    };
    let output: Tensor4D<i8, 1, 2, 1, 2, 1> = conv_2d(
        input,
        &weights,
        [1.0],
        [0],
        options,
        (Buffer2D::from_element(0.0), Buffer2D::from_element(1.0)),
    );
    assert_eq!(output.buffer[0][(0, 0)], [1, 6]);
    assert_eq!(output.buffer[0][(1, 0)], [6, 6]);
}

#[test]
fn depthwise_centered_products_preserve_quantization_and_boundary_padding() {
    use microflow::activation::FusedActivation;
    use microflow::ops::{depthwise_conv_2d, DepthwiseConv2DOptions};
    let input: Tensor4D<i8, 1, 4, 4, 2, 1> = Tensor4D::new(
        [Buffer2D::from_fn(|r, c| [(r * 4 + c + 1) as i8, 3])],
        [1.0],
        [3],
    );
    let weights: Tensor4D<i8, 1, 3, 3, 2, 2> =
        Tensor4D::new([Buffer2D::from_element([2, 0])], [1.0, 1.0], [1, -2]);
    let output: Tensor4D<i8, 1, 2, 2, 2, 1> = depthwise_conv_2d(
        input,
        &weights,
        [1.0],
        [0],
        DepthwiseConv2DOptions {
            fused_activation: FusedActivation::None,
            view_padding: TensorViewPadding::Same,
            strides: (2, 2),
        },
        (Buffer2D::from_element(0.0), Buffer2D::from_element(1.0)),
    );
    // The first 3x3 contains 1,2,3,5,6,7,9,10,11: sum=54, subtract
    // nine input zero points (27), multiply by centered weight (1).
    // Bottom/right padding is the input zero point, not raw numeric zero.
    assert_eq!(output.buffer[0][(0, 0)], [27, 0]);
    assert_eq!(output.buffer[0][(0, 1)], [27, 0]);
    assert_eq!(output.buffer[0][(1, 0)], [54, 0]);
    assert_eq!(output.buffer[0][(1, 1)], [42, 0]);
}
