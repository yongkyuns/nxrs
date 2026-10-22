"""Generate a fixed PICO numerical graph from the hash-pinned ESP-DL FlatBuffer.

Only build-time code reads the file format. No generic loader, encrypted model,
flash driver, RTOS, or mutable model download enters the target binary.
"""
from __future__ import annotations
import struct, math

class FlatBuffer:
    def __init__(self, data):
        if data[:8] != b'EDL2\0\0\0\0':
            raise ValueError('expected unencrypted EDL2 model')
        self.data = data[16:]
    def number(self, p, fmt='<I'):
        return struct.unpack_from(fmt, self.data, p)[0]
    def field(self, t, n):
        v = t - self.number(t, '<i'); slot = v + 4 + n * 2
        offset = self.number(slot, '<H') if slot < v + self.number(v, '<H') else 0
        return t + offset if offset else 0
    def scalar(self, t, n, fmt='<i', default=0):
        p = self.field(t,n)
        return self.number(p,fmt) if p else default
    def reference(self,t,n):
        p = self.field(t,n)
        return p + self.number(p) if p else 0
    def string_at(self,p):
        return self.data[p+4:p+4+self.number(p)].decode('utf-8')
    def string(self,t,n):
        p=self.reference(t,n)
        return self.string_at(p) if p else ''
    def vector(self,t,n):
        p=self.reference(t,n)
        return [p+4+i*4+self.number(p+4+i*4) for i in range(self.number(p))] if p else []
    def array(self,t,n,fmt='<q'):
        p=self.reference(t,n); size=struct.calcsize(fmt)
        return [self.number(p+4+i*size,fmt) for i in range(self.number(p))] if p else []
    def raw(self,t):
        p=self.reference(t,8)
        return self.data[p+4:p+4+self.number(p)*16] if p else b''
    def attributes(self,n):
        result={}
        for a in self.vector(n,5):
            name=self.string(a,0); kind=self.scalar(a,3)
            if kind==7: value=self.array(a,11)
            elif kind==2: value=self.scalar(a,5,'<q')
            elif kind==1: value=self.scalar(a,4,'<f')
            elif kind==3:
                p=self.reference(a,6);value=self.data[p+4:p+4+self.number(p)].decode()
            else: raise ValueError('unsupported fixed-model attribute')
            result[name]=value
        return result

def unpack_filter(t,group):
    """Inverse ESP-PPQ NHWC16 packing, retaining the same quantized weights.

    Scalar SDK convolution consumes NHWC; scalar depthwise consumes HWCN.
    Complete output-channel blocks interleave 16 lanes. Tail channels retain
    NHWC (depthwise tails interleave channels at each spatial position).
    """
    h,w,c,n=t['shape']
    if group>1: n,c=c,n
    data=t['data']; output=bytearray(h*w*c*n); block=n//16*16
    for oc in range(n):
        for k in range(h*w*c):
            if oc<block: source=(oc//16*h*w*c+k)*16+oc%16
            elif group==1: source=block*h*w*c+(oc-block)*h*w*c+k
            else: source=block*h*w*c+k*(n-block)+(oc-block)
            output[oc*h*w*c+k]=data[source]
    if group>1:
        output=bytearray(output[oc*h*w+k] for k in range(h*w) for oc in range(n))
    return bytes(output)

def read(data):
    f=FlatBuffer(data); graph=f.reference(f.number(0),7)
    params={f.string(t,6):dict(shape=f.array(t,0),exp=f.array(t,13)[0],dtype=f.scalar(t,1),data=f.raw(t)) for t in f.vector(graph,2)}
    variables={}
    for t in f.vector(graph,4)+f.vector(graph,5)+f.vector(graph,6):
        name=f.string(t,0)
        if not name: continue
        tensor=f.reference(f.reference(t,1),1)
        shape=[f.scalar(f.reference(d,0),1,'<q') for d in f.vector(f.reference(tensor,1),0)]
        if len(shape)!=4 or shape[0]!=1 or any(v<=0 for v in shape): raise ValueError('unsupported shape')
        variables[name]=dict(shape=shape,exp=f.array(t,3)[0])
    nodes=[dict(name=f.string(n,2),op=f.string(n,3),ins=[f.string_at(p) for p in f.vector(n,0)],out=f.string_at(f.vector(n,1)[0]),attrs=f.attributes(n)) for n in f.vector(graph,0)]
    if len(nodes)!=114 or variables['input']!={'shape':[1,224,224,3],'exp':1}: raise ValueError('unexpected PICO graph')
    for stage,side in enumerate((28,14,7)):
        expected_score={'shape':[1,side,side,1],'exp':-8 if stage==0 else -7}
        expected_box={'shape':[1,side,side,32],'exp':-4 if stage==2 else -3}
        if variables.get(f'score{stage}')!=expected_score or variables.get(f'bbox{stage}')!=expected_box:
            raise ValueError('unexpected PICO output protocol')
    return params,variables,nodes

def plan(variables,nodes):
    """First-fit lifetime plan. Output allocation precedes input retirement."""
    outputs=['score0','score1','score2','bbox0','bbox1','bbox2']
    last={name:-1 for name in variables}
    for i,node in enumerate(nodes):
        for name in node['ins']:
            if name in variables:last[name]=i
    for name in outputs:last[name]=len(nodes)
    offsets={}; live={}; high=0
    def allocate(name):
        nonlocal high
        size=(math.prod(variables[name]['shape'])+15)//16*16
        start=0
        for p,n in sorted(live.values()):
            if start+size<=p:break
            start=max(start,p+n)
        offsets[name]=start;live[name]=(start,size);high=max(high,start+size)
    allocate('input')
    for i,node in enumerate(nodes):
        if any(name in variables and name not in live for name in node['ins']):raise ValueError('input lifetime lost')
        allocate(node['out'])
        for name in list(live):
            if last[name]==i:del live[name]
    if set(offsets)!=set(variables):raise ValueError('unused or undefined variable')
    return offsets,high

def generate(model,destination,optimized):
    params,variables,nodes=read(model)
    offsets,arena=plan(variables,nodes)
    ids={name:i for i,name in enumerate(variables)}
    pids={name:10000000+i for i,name in enumerate(params)}
    def values(v):return '{'+','.join(map(str,v))+'}'
    code=[f'#include "dl_module_{name}.hpp"' for name in ['conv','mul','global_average_pool','concat','resize','lut']]
    code+=['#include <memory>','using namespace dl;','using namespace dl::module;']
    for name,t in params.items():
        data=t['data']
        owner=next((n for n in nodes if n['op']=='Conv' and n['ins'][1]==name),None)
        if owner and not optimized:data=unpack_filter(t,owner['attrs']['group'])
        code.append(f'alignas(16) static const unsigned char p{pids[name]}[]={values(data)};')
    code+=['struct Engine {','std::unique_ptr<void,decltype(&free)> arena;','ModelContext context;','std::vector<std::unique_ptr<Module>> ops;', 'explicit Engine(void *memory):arena(memory,free) {']
    for name,t in variables.items():
        code.append(f'context.push_back_tensor(new TensorBase({values(t["shape"])},static_cast<char*>(arena.get())+{offsets[name]},{t["exp"]},DATA_TYPE_INT8,false),false);')
    for name,t in params.items():
        dtype={3:'DATA_TYPE_INT8',6:'DATA_TYPE_INT32',1:'DATA_TYPE_FLOAT'}[t['dtype']]
        code.append(f'context.push_back_tensor(new TensorBase({values(t["shape"])},p{pids[name]},{t["exp"]},{dtype},false),true);')
    for node in nodes:
        a=node['attrs'];op=node['op'];ins=node['ins'];out=node['out']
        if op=='Conv':
            if a['dilations']!=[1,1] or a['activation'] not in ['Relu','Linear']:raise ValueError('unsupported convolution')
            pads=a['pads'];init=f'new Conv({"ReLU" if a["activation"]=="Relu" else "Linear"},{values([pads[0],pads[2],pads[1],pads[3]])},{{1,1}},{values(a["strides"])},nullptr,{a["group"]},QUANT_TYPE_SYMM_8BIT)'
        elif op=='Sigmoid':
            # LUT owns this separate TensorBase view. Context owns the original.
            lut=params[a['lut']]
            init=f'new LUT(nullptr,new TensorBase({values(lut["shape"])},p{pids[a["lut"]]},{lut["exp"]},DATA_TYPE_INT8,false),MODULE_NON_INPLACE,QUANT_TYPE_SYMM_8BIT)'
        elif op=='Mul':init='new Mul(nullptr,MODULE_NON_INPLACE,QUANT_TYPE_SYMM_8BIT)'
        elif op=='GlobalAveragePool':init='new GlobalAveragePool(nullptr,QUANT_TYPE_SYMM_8BIT)'
        elif op=='Concat':
            if a['axis']!=3:raise ValueError('unsupported concat axis')
            init='new Concat(nullptr,3,QUANT_TYPE_SYMM_8BIT)'
        elif op=='Resize':
            if a['mode']!='nearest' or a['nearest_mode']!='floor' or a['coordinate_transformation_mode']!='asymmetric':raise ValueError('unsupported resize')
            if variables[out]['shape'][1:3]!=[2*v for v in variables[ins[0]]['shape'][1:3]]:raise ValueError('unsupported resize factor')
            init='new Resize(nullptr,RESIZE_NEAREST,{1,1,2,2},{},false,QUANT_TYPE_SYMM_8BIT)';ins=ins[:1]
        else:raise ValueError('unsupported PICO operator: '+op)
        code.append('{ auto *op='+init+';')
        for name in ins:code.append(f'op->m_inputs_index.push_back({ids.get(name,pids.get(name))});')
        code.append(f'op->m_outputs_index.push_back({ids[out]});')
        shapes='{'+','.join(values((variables if name in variables else params)[name]['shape']) for name in ins)+'}'
        code.append(f'std::vector<std::vector<int>> shapes={shapes}; if(op->get_output_shape(shapes)!=std::vector<std::vector<int>>{{{values(variables[out]["shape"])}}})abort(); ops.emplace_back(op); }}')
    code+=['}', 'void run(){for(auto &op:ops)op->forward(&context,RUNTIME_MODE_SINGLE_CORE);}', '};',f'extern "C" void *nxrs_pico_create() {{void*arena=tool::calloc_aligned(1,{arena},MALLOC_CAP_DEFAULT);if(!arena)return nullptr;return new Engine(arena);}}','extern "C" void nxrs_pico_destroy(void *p){delete static_cast<Engine*>(p);}',f'extern "C" void nxrs_pico_run(void*p,const int8_t*input,int8_t*output){{auto&e=*static_cast<Engine*>(p);memcpy(e.context.get_tensor({ids["input"]})->data,input,150528);e.run();int offset=0;']
    for name in ['score0','score1','score2','bbox0','bbox1','bbox2']:
        code.append(f'{{auto*t=e.context.get_tensor({ids[name]});memcpy(output+offset,t->data,t->size);offset+=t->size;}}')
    code+=['}']
    destination.write_text('\n'.join(code)+'\n')
    return {'arena_bytes':arena,'model_input_bytes':150528,'model_output_bytes':33957,'operators':len(nodes),'optimized':optimized}
