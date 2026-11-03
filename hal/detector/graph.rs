//! Build-time reader/generator for the exact pinned EDL2 graph; no loader enters firmware.
use nxrs_build::json::{json, Value};
use nxrs_build::Result;
use std::{collections::BTreeMap, path::Path};
struct Flat<'a>(&'a [u8]);
impl Flat<'_> {
    fn data(&self, p: usize, n: usize) -> Result<&[u8]> {
        self.0
            .get(p..p.checked_add(n).ok_or("model offset overflow")?)
            .ok_or("truncated model".into())
    }
    fn u16(&self, p: usize) -> Result<u16> {
        Ok(u16::from_le_bytes(self.data(p, 2)?.try_into().unwrap()))
    }
    fn u32(&self, p: usize) -> Result<u32> {
        Ok(u32::from_le_bytes(self.data(p, 4)?.try_into().unwrap()))
    }
    fn i32(&self, p: usize) -> Result<i32> {
        Ok(self.u32(p)? as i32)
    }
    fn i64(&self, p: usize) -> Result<i64> {
        Ok(i64::from_le_bytes(self.data(p, 8)?.try_into().unwrap()))
    }
    fn field(&self, t: usize, n: usize) -> Result<usize> {
        let v = t
            .checked_add_signed(-(self.i32(t)? as isize))
            .ok_or("invalid model vtable")?;
        let slot = v + 4 + n * 2;
        let offset = if slot < v + self.u16(v)? as usize {
            self.u16(slot)? as usize
        } else {
            0
        };
        Ok(if offset == 0 { 0 } else { t + offset })
    }
    fn scalar(&self, t: usize, n: usize) -> Result<i32> {
        let p = self.field(t, n)?;
        if p == 0 {
            Ok(0)
        } else {
            self.i32(p)
        }
    }
    fn long(&self, t: usize, n: usize) -> Result<i64> {
        let p = self.field(t, n)?;
        if p == 0 {
            Ok(0)
        } else {
            self.i64(p)
        }
    }
    fn reference(&self, t: usize, n: usize) -> Result<usize> {
        let p = self.field(t, n)?;
        if p == 0 {
            Ok(0)
        } else {
            p.checked_add(self.u32(p)? as usize)
                .ok_or("model reference overflow".into())
        }
    }
    fn text(&self, p: usize) -> Result<String> {
        Ok(
            std::str::from_utf8(self.data(p + 4, self.u32(p)? as usize)?)
                .map_err(|e| e.to_string())?
                .into(),
        )
    }
    fn string(&self, t: usize, n: usize) -> Result<String> {
        let p = self.reference(t, n)?;
        if p == 0 {
            Ok(String::new())
        } else {
            self.text(p)
        }
    }
    fn vector(&self, t: usize, n: usize) -> Result<Vec<usize>> {
        let p = self.reference(t, n)?;
        if p == 0 {
            return Ok(vec![]);
        }
        let count = self.u32(p)? as usize;
        self.data(p + 4, count.checked_mul(4).ok_or("model vector overflow")?)?;
        (0..count)
            .map(|i| {
                let offset = p + 4 + i * 4;
                Ok(offset + self.u32(offset)? as usize)
            })
            .collect()
    }
    fn array(&self, t: usize, n: usize) -> Result<Vec<i64>> {
        let p = self.reference(t, n)?;
        if p == 0 {
            return Ok(vec![]);
        }
        let count = self.u32(p)? as usize;
        self.data(p + 4, count.checked_mul(8).ok_or("model array overflow")?)?;
        (0..count).map(|i| self.i64(p + 4 + i * 8)).collect()
    }
    fn raw(&self, t: usize) -> Result<Vec<u8>> {
        let p = self.reference(t, 8)?;
        if p == 0 {
            return Ok(vec![]);
        }
        Ok(self
            .data(
                p + 4,
                (self.u32(p)? as usize)
                    .checked_mul(16)
                    .ok_or("model data overflow")?,
            )?
            .into())
    }
    fn attributes(&self, t: usize) -> Result<Value> {
        let mut a = nxrs_build::json::Map::new();
        for item in self.vector(t, 5)? {
            let name = self.string(item, 0)?;
            let value = match self.scalar(item, 3)? {
                7 => json!(self.array(item, 11)?),
                2 => json!(self.long(item, 5)?),
                1 => json!(f32::from_bits(self.u32(self.field(item, 4)?)?)),
                3 => json!(self.text(self.reference(item, 6)?)?),
                _ => return Err("unsupported fixed-model attribute".into()),
            };
            a.insert(name, value);
        }
        Ok(Value::Object(a))
    }
}
#[derive(Clone)]
struct Tensor {
    shape: Vec<i64>,
    exp: i64,
    dtype: i32,
    data: Vec<u8>,
}
struct Node {
    op: String,
    ins: Vec<String>,
    out: String,
    attrs: Value,
}
type Tensors = Vec<(String, Tensor)>;
fn tensor<'a>(t: &'a Tensors, name: &str) -> Result<&'a Tensor> {
    t.iter()
        .find(|(n, _)| n == name)
        .map(|(_, t)| t)
        .ok_or_else(|| format!("undefined tensor {name}"))
}
fn parse(model: &[u8]) -> Result<(Tensors, Tensors, Vec<Node>)> {
    if model.get(..8) != Some(b"EDL2\0\0\0\0") {
        return Err("expected unencrypted EDL2 model".into());
    }
    let f = Flat(model.get(16..).ok_or("truncated model")?);
    let graph = f.reference(f.u32(0)? as usize, 7)?;
    let mut params = vec![];
    for t in f.vector(graph, 2)? {
        params.push((
            f.string(t, 6)?,
            Tensor {
                shape: f.array(t, 0)?,
                exp: *f
                    .array(t, 13)?
                    .first()
                    .ok_or("missing parameter exponent")?,
                dtype: f.scalar(t, 1)?,
                data: f.raw(t)?,
            },
        ));
    }
    let mut vars: Tensors = vec![];
    for t in f
        .vector(graph, 4)?
        .into_iter()
        .chain(f.vector(graph, 5)?)
        .chain(f.vector(graph, 6)?)
    {
        let name = f.string(t, 0)?;
        if name.is_empty() {
            continue;
        }
        let ty = f.reference(f.reference(t, 1)?, 1)?;
        let shape = f
            .vector(f.reference(ty, 1)?, 0)?
            .into_iter()
            .map(|d| f.long(f.reference(d, 0)?, 1))
            .collect::<Result<Vec<_>>>()?;
        if shape.len() != 4 || shape[0] != 1 || shape.iter().any(|v| *v <= 0) {
            return Err("unsupported model shape".into());
        }
        let t = Tensor {
            shape,
            exp: *f.array(t, 3)?.first().ok_or("missing tensor exponent")?,
            dtype: 3,
            data: vec![],
        };
        if let Some((_, previous)) = vars.iter_mut().find(|(n, _)| n == &name) {
            *previous = t;
        } else {
            vars.push((name, t));
        }
    }
    let mut nodes = vec![];
    for n in f.vector(graph, 0)? {
        let ins = f
            .vector(n, 0)?
            .into_iter()
            .map(|p| f.text(p))
            .collect::<Result<Vec<_>>>()?;
        let outs = f.vector(n, 1)?;
        nodes.push(Node {
            op: f.string(n, 3)?,
            ins,
            out: f.text(*outs.first().ok_or("node output missing")?)?,
            attrs: f.attributes(n)?,
        });
    }
    if nodes.len() != 114
        || tensor(&vars, "input")?.shape != [1, 224, 224, 3]
        || tensor(&vars, "input")?.exp != 1
    {
        return Err("unexpected PICO graph".into());
    }
    for (stage, side) in [28, 14, 7].into_iter().enumerate() {
        for (name, channels, exp) in [
            (format!("score{stage}"), 1, if stage == 0 { -8 } else { -7 }),
            (format!("bbox{stage}"), 32, if stage == 2 { -4 } else { -3 }),
        ] {
            let t = tensor(&vars, &name)?;
            if t.shape != [1, side, side, channels] || t.exp != exp {
                return Err("unexpected PICO output protocol".into());
            }
        }
    }
    Ok((params, vars, nodes))
}
fn size(t: &Tensor) -> Result<usize> {
    t.shape.iter().try_fold(1usize, |n, v| {
        usize::try_from(*v)
            .ok()
            .and_then(|v| n.checked_mul(v))
            .ok_or("model shape overflow".into())
    })
}
fn plan(vars: &Tensors, nodes: &[Node]) -> Result<(BTreeMap<String, usize>, usize)> {
    let mut first = BTreeMap::from([("input".to_owned(), -1isize)]);
    let mut last: BTreeMap<_, _> = vars.iter().map(|(n, _)| (n.clone(), -1isize)).collect();
    for (i, node) in nodes.iter().enumerate() {
        for name in &node.ins {
            if last.contains_key(name) && !first.contains_key(name) {
                return Err("input lifetime lost".into());
            }
        }
        first.insert(node.out.clone(), i as isize);
        for name in &node.ins {
            if let Some(v) = last.get_mut(name) {
                *v = i as isize;
            }
        }
    }
    for name in ["score0", "score1", "score2", "bbox0", "bbox1", "bbox2"] {
        *last.get_mut(name).ok_or("model output missing")? = nodes.len() as isize;
    }
    if first.len() != vars.len() || vars.iter().any(|(name, _)| !first.contains_key(name)) {
        return Err("unused or undefined model variable".into());
    }
    let mut sizes = BTreeMap::new();
    for (name, t) in vars {
        sizes.insert(name.clone(), size(t)?.div_ceil(16) * 16);
        last.insert(name.clone(), last[name].max(first[name]));
    }
    let mut order: Vec<_> = vars.iter().map(|(n, _)| n).collect();
    order.sort_by_key(|n| std::cmp::Reverse(sizes[*n]));
    let mut offsets: BTreeMap<String, usize> = BTreeMap::new();
    let mut high = 0;
    for name in order {
        let mut live: Vec<_> = offsets
            .iter()
            .filter(|(other, _)| first[name] <= last[*other] && first[*other] <= last[name])
            .map(|(other, p)| (*p, sizes[other]))
            .collect();
        live.sort();
        let mut start = 0;
        for (p, n) in live {
            if start + sizes[name] <= p {
                break;
            }
            start = start.max(p + n);
        }
        offsets.insert(name.clone(), start);
        high = high.max(start + sizes[name]);
    }
    Ok((offsets, high))
}
fn unpack(t: &Tensor, group: i64) -> Result<Vec<u8>> {
    if t.shape.len() != 4 {
        return Err("unsupported filter shape".into());
    }
    let (h, w, mut c, mut n) = (
        t.shape[0] as usize,
        t.shape[1] as usize,
        t.shape[2] as usize,
        t.shape[3] as usize,
    );
    if group > 1 {
        std::mem::swap(&mut n, &mut c);
    }
    let mut out = vec![0; h * w * c * n];
    let block = n / 16 * 16;
    for oc in 0..n {
        for k in 0..h * w * c {
            let p = if oc < block {
                (oc / 16 * h * w * c + k) * 16 + oc % 16
            } else if group == 1 {
                block * h * w * c + (oc - block) * h * w * c + k
            } else {
                block * h * w * c + k * (n - block) + (oc - block)
            };
            out[oc * h * w * c + k] = *t.data.get(p).ok_or("truncated model filter")?;
        }
    }
    if group > 1 {
        out = (0..h * w)
            .flat_map(|k| (0..n).map(move |oc| (oc, k)))
            .map(|(oc, k)| out[oc * h * w + k])
            .collect();
    }
    Ok(out)
}
fn values<T: std::fmt::Display>(v: &[T]) -> String {
    format!(
        "{{{}}}",
        v.iter()
            .map(ToString::to_string)
            .collect::<Vec<_>>()
            .join(",")
    )
}
pub fn generate(model: &[u8], destination: &Path, optimized: bool) -> Result<Value> {
    let (params, vars, nodes) = parse(model)?;
    let (offsets, arena) = plan(&vars, &nodes)?;
    let ids: BTreeMap<_, _> = vars
        .iter()
        .enumerate()
        .map(|(i, (n, _))| (n.clone(), i))
        .collect();
    let pids: BTreeMap<_, _> = params
        .iter()
        .enumerate()
        .map(|(i, (n, _))| (n.clone(), 10000000 + i))
        .collect();
    let mut code: Vec<String> = [
        "conv",
        "mul",
        "global_average_pool",
        "concat",
        "resize",
        "lut",
    ]
    .iter()
    .map(|n| format!("#include \"dl_module_{n}.hpp\""))
    .collect();
    code.extend(
        [
            "#include <memory>",
            "using namespace dl;",
            "using namespace dl::module;",
        ]
        .map(str::to_owned),
    );
    let mut poff = BTreeMap::new();
    let mut parameter_bytes = 0;
    for (name, t) in &params {
        let owner = nodes
            .iter()
            .find(|n| n.op == "Conv" && n.ins.get(1) == Some(name));
        let data = if let Some(n) = owner.filter(|_| !optimized) {
            unpack(
                t,
                n.attrs["group"]
                    .as_i64()
                    .ok_or("missing convolution group")?,
            )?
        } else {
            t.data.clone()
        };
        code.push(format!(
            "alignas(16) static const unsigned char p{}[]={};",
            pids[name],
            values(&data)
        ));
        poff.insert(name.clone(), parameter_bytes);
        parameter_bytes += data.len().div_ceil(16) * 16;
    }
    code.extend(
        [
            "struct Engine {",
            "std::unique_ptr<void,decltype(&free)> arena;",
            "std::unique_ptr<void,decltype(&free)> parameters;",
            "ModelContext context;",
            "std::vector<std::unique_ptr<Module>> ops;",
            "Engine(void *memory,void *weights):arena(memory,free),parameters(weights,free) {",
        ]
        .map(str::to_owned),
    );
    for (name, _) in &params {
        code.push(format!(
            "memcpy(static_cast<char*>(parameters.get())+{},p{},sizeof(p{}));",
            poff[name], pids[name], pids[name]
        ));
    }
    for (name, t) in &vars {
        code.push(format!("context.push_back_tensor(new TensorBase({},static_cast<char*>(arena.get())+{},{},DATA_TYPE_INT8,false),false);",values(&t.shape),offsets[name],t.exp));
    }
    for (name, t) in &params {
        let dtype = match t.dtype {
            3 => "DATA_TYPE_INT8",
            6 => "DATA_TYPE_INT32",
            1 => "DATA_TYPE_FLOAT",
            _ => return Err("unsupported parameter dtype".into()),
        };
        code.push(format!("context.push_back_tensor(new TensorBase({},static_cast<char*>(parameters.get())+{},{},{dtype},false),true);",values(&t.shape),poff[name],t.exp));
    }
    for node in &nodes {
        let a = &node.attrs;
        let mut ins = node.ins.clone();
        let out = &node.out;
        if a["quant_type"] != "S8" {
            return Err("fixed graph requires S8 operators".into());
        }
        let arr = |key: &str| -> Result<Vec<i64>> {
            a[key]
                .as_array()
                .ok_or("missing operator attribute")?
                .iter()
                .map(|v| v.as_i64().ok_or("invalid integer attribute".into()))
                .collect()
        };
        let init = match node.op.as_str() {
            "Conv" => {
                if arr("dilations")? != [1, 1]
                    || !["Relu", "Linear"].iter().any(|s| a["activation"] == *s)
                {
                    return Err("unsupported convolution".into());
                }
                let pads = arr("pads")?;
                if pads.len() != 4 {
                    return Err("invalid convolution pads".into());
                }
                format!(
                    "new Conv({},{},{{1,1}},{},nullptr,{},QUANT_TYPE_SYMM_8BIT)",
                    if a["activation"] == "Relu" {
                        "ReLU"
                    } else {
                        "Linear"
                    },
                    values(&[pads[0], pads[2], pads[1], pads[3]]),
                    values(&arr("strides")?),
                    a["group"].as_i64().ok_or("invalid convolution group")?
                )
            }
            "Sigmoid" => {
                let name = a["lut"].as_str().ok_or("missing LUT")?;
                let lut = tensor(&params, name)?;
                format!("new LUT(nullptr,new TensorBase({},static_cast<char*>(parameters.get())+{},{},DATA_TYPE_INT8,false),MODULE_NON_INPLACE,QUANT_TYPE_SYMM_8BIT)",values(&lut.shape),poff[name],lut.exp)
            }
            "Mul" => "new Mul(nullptr,MODULE_NON_INPLACE,QUANT_TYPE_SYMM_8BIT)".into(),
            "GlobalAveragePool" => "new GlobalAveragePool(nullptr,QUANT_TYPE_SYMM_8BIT)".into(),
            "Concat" => {
                if a["axis"] != 3 {
                    return Err("unsupported concat axis".into());
                }
                "new Concat(nullptr,3,QUANT_TYPE_SYMM_8BIT)".into()
            }
            "Resize" => {
                if a["mode"] != "nearest"
                    || a["nearest_mode"] != "floor"
                    || a["coordinate_transformation_mode"] != "asymmetric"
                    || tensor(&vars, out)?.shape[1..3]
                        != tensor(&vars, &ins[0])?.shape[1..3]
                            .iter()
                            .map(|v| v * 2)
                            .collect::<Vec<_>>()
                {
                    return Err("unsupported resize".into());
                }
                ins.truncate(1);
                "new Resize(nullptr,RESIZE_NEAREST,{1,1,2,2},{},false,QUANT_TYPE_SYMM_8BIT)".into()
            }
            _ => return Err(format!("unsupported PICO operator {}", node.op)),
        };
        code.push(format!("{{ auto *op={init};"));
        for name in &ins {
            let id = ids
                .get(name)
                .or_else(|| pids.get(name))
                .ok_or("undefined operator input")?;
            code.push(format!("op->m_inputs_index.push_back({id});"));
        }
        code.push(format!("op->m_outputs_index.push_back({});", ids[out]));
        let shapes = ins
            .iter()
            .map(|name| {
                tensor(
                    if ids.contains_key(name) {
                        &vars
                    } else {
                        &params
                    },
                    name,
                )
                .map(|t| values(&t.shape))
            })
            .collect::<Result<Vec<_>>>()?
            .join(",");
        code.push(format!("std::vector<std::vector<int>> shapes={{{shapes}}}; if(op->get_output_shape(shapes)!=std::vector<std::vector<int>>{{{}}})abort(); ops.emplace_back(op); }}",values(&tensor(&vars,out)?.shape)));
    }
    code.extend(["}","~Engine(){ops.clear();for(auto*t:context.m_variables)delete t;for(auto*t:context.m_parameters)delete t;context.clear();}","void run(){for(auto &op:ops)op->forward(&context,RUNTIME_MODE_SINGLE_CORE);}","};"].map(str::to_owned));
    code.push(format!("extern \"C\" void *pico_create() {{void*arena=tool::calloc_aligned(1,{arena},MALLOC_CAP_DEFAULT);if(!arena)return nullptr;void*weights=tool::calloc_aligned(1,{parameter_bytes},MALLOC_CAP_DEFAULT);if(!weights){{free(arena);return nullptr;}}return new Engine(arena,weights);}}"));
    code.push("extern \"C\" void pico_destroy(void *p){delete static_cast<Engine*>(p);}".into());
    code.push(format!("extern \"C\" void pico_run(void*p,const int8_t*input,int8_t*output){{auto&e=*static_cast<Engine*>(p);memcpy(e.context.get_tensor({})->data,input,150528);e.run();int offset=0;",ids["input"]));
    for name in ["score0", "score1", "score2", "bbox0", "bbox1", "bbox2"] {
        code.push(format!("{{auto*t=e.context.get_tensor({});memcpy(output+offset,t->data,t->size);offset+=t->size;}}",ids[name]));
    }
    code.push("}".into());
    std::fs::write(destination, code.join("\n") + "\n").map_err(|e| e.to_string())?;
    Ok(
        json!({"arena_bytes":arena,"parameter_bytes":parameter_bytes,"model_input_bytes":150528,"model_output_bytes":33957,"operators":nodes.len(),"optimized":optimized}),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    fn t(shape: &[i64], data: Vec<u8>) -> Tensor {
        Tensor {
            shape: shape.into(),
            exp: 0,
            dtype: 3,
            data,
        }
    }
    #[test]
    fn inclusive_operator_lifetimes_prevent_input_output_aliasing() {
        let names = [
            "input",
            "temporary0",
            "temporary1",
            "score0",
            "score1",
            "score2",
            "bbox0",
            "bbox1",
            "bbox2",
        ];
        let vars = names
            .iter()
            .map(|n| (n.to_string(), t(&[1, 1, 1, 16], vec![])))
            .collect();
        let pairs = [
            ("input", "temporary0"),
            ("temporary0", "score0"),
            ("input", "temporary1"),
            ("temporary1", "score1"),
            ("input", "score2"),
            ("input", "bbox0"),
            ("input", "bbox1"),
            ("input", "bbox2"),
        ];
        let nodes = pairs
            .iter()
            .map(|(i, o)| Node {
                op: "fixture".into(),
                ins: vec![i.to_string()],
                out: o.to_string(),
                attrs: json!({}),
            })
            .collect::<Vec<_>>();
        let (offsets, _) = plan(&vars, &nodes).unwrap();
        assert_eq!(offsets["temporary0"], offsets["temporary1"]);
        assert_ne!(offsets["temporary0"], offsets["score0"]);
        assert_ne!(offsets["temporary1"], offsets["score1"]);
    }
    #[test]
    fn scalar_filter_unpacking_preserves_full_blocks_and_tails() {
        let data = (0u8..38).collect::<Vec<_>>();
        let ordinary = unpack(&t(&[2, 1, 1, 19], data.clone()), 1).unwrap();
        let expected = (0u8..16)
            .flat_map(|c| [c, c + 16])
            .chain(32..38)
            .collect::<Vec<_>>();
        assert_eq!(ordinary, expected);
        let depthwise = unpack(&t(&[2, 1, 19, 1], data), 19).unwrap();
        let expected = (0u8..16)
            .chain(32..35)
            .chain(16..32)
            .chain(35..38)
            .collect::<Vec<_>>();
        assert_eq!(depthwise, expected);
    }
}
