"""RP2350 hot-code placement and scoped linked-image inspection on both boards."""
from pathlib import Path
import bisect,io,re,subprocess,tempfile,hashlib,json
AR = OBJCOPY = None
COMMON=re.compile(r'mpsc_comparison6engine(?:6worker|5offer|7payload)|mpsc_comparison8arrivals(?:4tick|5start|7release)|transport..(?:ordinary_std|Native)|^mp_(?:clock32|gate_post|gate_wait|queue_send|queue_receive|native_ticks|pulse_arm)$|^pulse_expired$|core..iter.*4next|core3num.*8div_ceil|mpsc_comparison5check|core4iter.*(?:7step_by|4next)|^OUTLINED_FUNCTION_|compiler_builtins.*mem|core5array8equality|mpsc_comparison8platform6native(?:10queue_send|13queue_receive|5clock|7irq_arm|8irq_post|9gate_wait|9gate_post|8priority|6policy)|^mem(?:cpy|move|set|cmp)$|^bcmp$|^__aeabi_mem(?:cpy|move|set|clr)')
NRUST=re.compile(r'std3sys12thread_local3key4racy7LazyKey5force|std3sys12thread_local2os12LocalPointer3get|std4sync4mpmc(?:5array|7counter)|std..sync..mpmc..array|std4sync4mpsc|std3sys4sync|__rust_nuttx_spin_|std6thread7current18current_or_unnamed|std6thread6Thread4park|std..thread..Thread|alloc11collections9vec_deque.*(?:13push_back_mut|9push_back|9pop_front)')
NATIVE=re.compile(r'^(?:nxsem_|nxsched_|wd_|sched_lock$|sched_unlock$|sched_idletask$|irq_dispatch$|pthread_getspecific$|tls_get_info$|nxsig_pendingset$|nxsig_nandset$|nxsig_addset$|sigemptyset$|sigisemptyset$|clock_systime|clock_get_sched_ticks|clock_increase_sched_ticks|up_switch_context$|up_fullcontextrestore$|up_saveusercontext$|arm_|exception_common)')
ZARCH=re.compile(r'^(?:arch_cpu_idle$|arch_irq_unlock_outlined$|z_arm_pendsv$|z_arm_svc$|z_arm_exc_exit$|z_arm_int_exit$|sys_clock_(?:isr|set_timeout|elapsed|cycle_get))')
def sections(data):
 from elftools.elf.elffile import ELFFile
 e=ELFFile(io.BytesIO(data));tab=e.get_section_by_name('.symtab');names={}
 if tab:
  for s in tab.iter_symbols():
   if isinstance(s['st_shndx'],int):names.setdefault(s['st_shndx'],[]).append(s.name)
 return [(i,s.name,s.data(),s['sh_flags'],names.get(i,[])) for i,s in enumerate(e.iter_sections()) if s['sh_type']!='SHT_NOBITS' and s['sh_flags']&4]
def transform(path,kind,all_kernel=False):
 data=path.read_bytes()
 if not data.startswith(b'\x7fELF'):return None
 before=sections(data);mapping={}
 for i,name,raw,flags,names in before:
  if not name.startswith('.text'):continue
  if all_kernel or any(COMMON.search(n) or (kind=='nuttx' and (NRUST.search(n) or NATIVE.search(n))) or (kind=='zephyr' and ZARCH.search(n)) for n in names):mapping[name]=('.time_critical.' if kind=='nuttx' else '.ramfunc.')+name
 # GCC can lower the startup zero/copy loops to libc calls. Only the
 # reset object's references need the XIP copies of these functions.
 bootstrap = kind == 'nuttx' and path.name == 'rp23xx_start.o'
 if not mapping and not bootstrap:return None
 out=path.with_suffix(path.suffix+'.placed');args=[OBJCOPY]
 if bootstrap:
  args.extend(['--redefine-sym', 'memset=mp_boot_memset',
               '--redefine-sym', 'memcpy=mp_boot_memcpy'])
 for old,new in mapping.items():args.extend(['--rename-section',old+'='+new])
 subprocess.run([*args,str(path),str(out)],check=True);after=sections(out.read_bytes());lookup={name:raw for _,name,raw,_,_ in after}
 assert len(before)==len(after)
 for _,name,raw,_,_ in before:assert lookup[mapping.get(name,name)]==raw,'instruction bytes changed'
 result=dict(object=path.name,sections=[dict(old=old,new=new,bytes=len(lookup[new]),sha256=hashlib.sha256(lookup[new]).hexdigest()) for old,new in mapping.items()],all_executable_section_bytes_preserved=True)
 if bootstrap:
  result['startup_memory_references'] = {'memset': 'mp_boot_memset', 'memcpy': 'mp_boot_memcpy'}
 out.replace(path);return result
def archive(path,kind,all_kernel=False):
 names=subprocess.check_output([AR,'t',str(path)],text=True).splitlines();assert len(names)==len(set(names)),('duplicate member names',path)
 assert all(Path(n).name==n and n not in ['.','..'] for n in names)
 with tempfile.TemporaryDirectory(prefix='mpsc-placement-') as d:
  d=Path(d);subprocess.run([AR,'x',str(path)],cwd=d,check=True);changed=[]
  for n in names:
   row=transform(d/n,kind,all_kernel)
   if row:changed.append(row)
  if not changed:return []
  target=d/'placed.a'
  for i in range(0,len(names),40):subprocess.run([AR,'rcD',str(target),*[str(d/n) for n in names[i:i+40]]],check=True,stdout=subprocess.DEVNULL)
  subprocess.run([AR,'s',str(target)],check=True);path.write_bytes(target.read_bytes());return changed


def prepare(root, prefix, env, zephyr=False, backend=None):
 global AR, OBJCOPY
 AR, OBJCOPY = prefix+'ar', prefix+'objcopy'
 root=Path(root).resolve();kind='zephyr' if zephyr else 'nuttx';rows=[]
 if zephyr:
  archives=list((root/'firmware').rglob('*.a'))
  archives.append(root/'rust/thumbv8m.main-none-eabi/release/libmpsc_comparison.a')
 else:
  archives=list((root/'firmware/nuttx/staging').glob('*.a'))
 for path in archives:
  resolved=path.resolve()
  if not resolved.is_relative_to(root):raise ValueError('archive escapes owned build')
  changed=archive(resolved,kind,zephyr and path.name in ['libkernel.a','libdrivers__timer.a'])
  if changed:rows.append(dict(archive=str(path.relative_to(root)),objects=changed))
 if not rows:raise ValueError('no hot sections selected')
 (root/'hot-placement.json').write_text(json.dumps(dict(section_names_only=zephyr,compiled_instruction_bytes_unchanged=True,selection=rows),indent=2)+'\n')
 if zephyr:
  subprocess.run(['cmake','--build',str(root/'firmware'),'--parallel','4'],env=env,check=True,stdout=subprocess.DEVNULL,timeout=300)
  elf=root/'firmware/zephyr/zephyr.elf'
 else:
  kernel=root/'firmware/nuttx'
  control=root/'link-vars.mk';control.write_text(".PHONY: mpsc-link-vars\nmpsc-link-vars:\n\t@echo '$(LINKLIBS)'\n")
  libs=subprocess.check_output(['make','-s','-C',str(kernel),'-f','Makefile','-f',str(control),'CROSSDEV='+prefix,'mpsc-link-vars'],env=env,text=True).strip()
  if not libs or not all(x.endswith('.a') and '/' not in x for x in libs.split()):raise ValueError('invalid native link libraries')
  elf=kernel/'nuttx';elf.unlink()
  with (root/'relink.log').open('w') as log:
   subprocess.run(['make','-C',str(kernel/'arch/arm/src'),'TOPDIR='+str(kernel),'APPDIR='+str(root/'firmware/apps'),'CROSSDEV='+prefix,'LINKLIBS='+libs,'-o','board/libboard.a','nuttx'],env=env,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=300)
  subprocess.run([OBJCOPY,'-O','binary',str(elf),str(kernel/'nuttx.bin')],env=env,check=True)
 # Preserve the existing std/Zephyr gate. The newly exposed NuttX native
 # transport is diagnostic until its additional public-mqueue call path has
 # been inspected; do not silently promote that entire dependency closure.
 audit(elf,prefix,root/'hot-audit.json',board='pico2w',backend=backend,
       strict=zephyr or backend!='native',nuttx_bootstrap=not zephyr)

rootpat=re.compile(r'mpsc_comparison::engine::(?:worker|offer)|mpsc_comparison::arrivals::(?:tick|release)|transport::(?:ordinary_std::Std|Native).*Channel.*::(?:send|receive)|^(?:mq|nxmq)_(?:send|receive)$|^mp_(?:clock32|gate_post|gate_wait|queue_send|queue_receive|native_ticks|pulse_arm)$|^pulse_expired$|^std::sync::mpmc::array::(?:SendSlot|RecvSlot)|^std::sys::sync::thread_parking.*Parker::unpark$|^(?:arm_doirq|arm_svcall|arm_exception|exception_common|up_switch_context|nxsched_process_timer|sys_clock_isr|z_arm_pendsv|z_arm_svc|z_arm_exc_exit|z_arm_int_exit|arch_cpu_idle)$')
def cold(n):
 if any(x in n for x in ['panicking::','unwrap_failed','__assert','zassert_fail','k_panic','::fatal','mpsc_comparison::line','core::fmt::',' as core::fmt::','engine::enter','mp_get_schedule','_Unwind','std::thread::Thread::new','std::thread::current::init_current','std::thread::current::id::get_or_init','LazyKey::lazy_init','::configure','mp_tid','std::time::Instant','Thread::park_timeout','ThreadId::new','MutexGuard<u64>','pthread_mutex_lock','pthread_mutex_unlock','pthread_mutex_destroy','pthread_setspecific','Mutex::init','Mutex::lock::fail','__rust_alloc_zeroed','__rust_no_alloc_shim','handle_alloc_error','up_schedule_sigaction','z_do_kernel_oops','std::process::abort','thread_local::guard::key::enable','thread_local::os::LocalPointer::set','std::io::Write::write_fmt','drop_in_place<core::result::Result<(),std::io::error::Error>>']):return True
 if any(x in n for x in ['mpmc::list::','mpmc::zero::','VecDeque<T,A>::grow','__rust_dealloc']):return True
 return 'alloc::sync::Arc' in n and 'drop_slow' in n
def memory_kind(address,board):
 ranges={'pico2w':((0x20000000,0x20082000),(0x10000000,0x14000000)),
         'esp32s3':((0x40370000,0x403e0000),(0x42000000,0x44000000))}
 internal,flash=ranges[board]
 if internal[0]<=address<internal[1]:return 'internal-code'
 if flash[0]<=address<flash[1]:return 'flash'
 # ROM and other addresses remain explicit. Absence from mapped flash is not
 # proof of SRAM placement or a qualified ROM call.
 return 'other'


IRQ_ROOTS=('mp_irq_probe_isr','mp_irq_probe_now','mp_irq_probe_arm','mpsc_irq_probe_entry')

def inspect_disassembly(out,board,backend=None,irq_probe=False):
 """Inspect observed direct paths; this is not a complete control-flow proof."""
 if board not in ('pico2w','esp32s3'):raise ValueError('unknown placement board')
 fns={};current=None
 instruction=re.compile(r'^\s*([0-9a-f]+):\s+((?:(?:[0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{4}|[0-9a-f]{2})\s+)+)([.a-zA-Z][\w.]*)\s*(.*)$')
 arm_direct=re.compile(r'^(?:blx?|b(?:eq|ne|cs|hs|cc|lo|mi|pl|vs|vc|hi|ls|ge|lt|gt|le)?|cbnz|cbz)(?:\.[wn])?$')
 # BREAK's numeric operands are trap codes, not branch destinations. Match
 # actual branch mnemonics rather than treating every b-prefixed opcode alike.
 xtensa_direct=re.compile(r'^(?:call(?:0|4|8|12)|j|b(?:all|any|bc|bci|bs|bsi|eq|eqi|eqz|ge|gei|geu|geui|gez|lt|lti|ltu|ltui|ltz|ne|nei|nez|nall|none|f|t)(?:\.n)?)$')
 for line in out.splitlines():
  m=re.match(r'^([0-9a-f]+) <(.+)>:$',line)
  if m:
   current=int(m[1],16)
   fns[current]={'name':m[2],'end':current,'edges':set(),'indirect':[]}
   continue
  if current is None:continue
  m=instruction.match(line)
  if not m:continue
  address=int(m[1],16);op=m[3];operands=m[4]
  f=fns[current];f['end']=max(f['end'],address+len(''.join(m[2].split()))//2)
  if 'veneer' in f['name'] and op=='.word':
   target=re.search(r'0x([0-9a-f]+)',operands)
   if target:f['edges'].add(int(target[1],16)&~1)
  direct=bool((arm_direct if board=='pico2w' else xtensa_direct).fullmatch(op))
  indirect=(board=='esp32s3' and bool(re.fullmatch(r'callx(?:0|4|8|12)|jx',op))) or (
      board=='pico2w' and op in ('blx','bx')
      and re.fullmatch(r'r\d+|ip|sp|pc|lr',operands.strip())
      and not (op=='bx' and operands.strip() in ('lr','r14')))
  if indirect:
   f['indirect'].append(dict(address=hex(address),instruction=op+' '+operands))
   continue
  if not direct:continue
  target=re.search(r'\b([0-9a-f]+)\s+<',operands)
  if target:f['edges'].add(int(target[1],16))
  else:
   target=re.fullmatch(r'(?:0x)?([0-9a-f]+)',operands.split(',')[-1].strip())
   if target:f['edges'].add(int(target[1],16))
 starts=sorted(fns)
 def owner(address):
  i=bisect.bisect_right(starts,address)-1
  if i>=0 and starts[i]<=address<fns[starts[i]]['end']:return starts[i]
  return address if address in fns else None
 roots=[a for a,f in fns.items() if (f['name'] in IRQ_ROOTS if irq_probe else rootpat.search(f['name']))]
 missing=[]
 if irq_probe:
  missing=[name for name in IRQ_ROOTS if not any(fns[a]['name']==name for a in roots)]
 elif not any('mpsc_comparison::engine::worker' in fns[a]['name'] for a in roots):
  missing.append('workload worker')
 if backend=='native' and not irq_probe:
  for name in ('mp_queue_send','mp_queue_receive'):
   if not any(fns[a]['name']==name for a in roots):missing.append(name)
 todo=list(roots);seen=set();parents={};outside=[];other=[];unresolved=[];indirect=[];excluded=[]
 for a in roots:
  kind=memory_kind(a,board)
  row=dict(name=fns[a]['name'],address=hex(a),caller='root')
  if kind=='flash':outside.append(row)
  elif kind=='other':other.append(row)
 while todo:
  a=todo.pop()
  if a in seen:continue
  seen.add(a);f=fns[a]
  # The existing std warmup exclusions do not establish that native queue
  # allocation/mutex dependencies are cold.
  is_cold=not irq_probe and cold(f['name']) and not (backend=='native' and any(
      name in f['name'] for name in ('pthread_mutex_lock','pthread_mutex_unlock','pthread_mutex_destroy')))
  if is_cold:
   excluded.append(f['name']);continue
  kind=memory_kind(a,board)
  row=dict(name=f['name'],address=hex(a),caller=fns[parents[a]]['name'] if a in parents else 'root')
  if kind=='flash':
   if not any(r['address']==row['address'] for r in outside):outside.append(row)
   continue
  if kind=='other':
   if not any(r['address']==row['address'] for r in other):other.append(row)
   continue
  indirect.extend(dict(function=f['name'],**call) for call in f['indirect'])
  for target in sorted(f['edges']):
   destination=owner(target)
   if destination is None:
    unresolved.append(dict(caller=f['name'],address=hex(target),memory=memory_kind(target,board)))
   elif destination not in seen:
    parents.setdefault(destination,a);todo.append(destination)
 direct_ok=bool(roots) and not (missing or outside or other or unresolved)
 return dict(schema='mpsc-hot-placement-v1',board=board,backend=backend,irq_probe=irq_probe,
             roots=[dict(name=fns[a]['name'],address=hex(a),memory=memory_kind(a,board)) for a in roots],
             missing_roots=missing,visited=len(seen),flash_boundaries=outside,
             other_memory_boundaries=other,unresolved_direct_targets=unresolved,
             indirect_calls=indirect,excluded_cold=sorted(set(excluded)),
             direct_paths_in_sram=direct_ok,indirect_calls_qualified=False,
             matched_placement_qualified=False,physical_timing_qualified=False,
             scope='observed direct branches up to non-internal or listed cold boundaries; indirect targets and actual executed paths remain unqualified')


def check_nuttx_bootstrap(disassembly):
 def function(name):
  match = re.search(rf'^([0-9a-f]+) <{name}>:\n((?:[^\n]+\n)+)', disassembly, re.M)
  if not match or memory_kind(int(match[1], 16), 'pico2w') != 'flash':
   raise ValueError(f'Pico bootstrap function {name} is missing from flash')
  return match[2]

 reset = function('__start')
 calls = re.findall(r'\bblx?(?:\.w)?\s+([0-9a-f]+)\s+<([^>]+)>', reset)
 if not any(target == 'nx_start' for _, target in calls) or re.search(r'\bblx\s+r\d+', reset):
  raise ValueError('Pico reset call sequence could not be verified')
 for address, target in calls:
  if (memory_kind(int(address, 16), 'pico2w') != 'flash' or target.endswith('_veneer')
      or re.fullmatch(r'mem(?:set|cpy)|__aeabi_mem.*', target)):
   raise ValueError(f'Pico reset calls unsafe bootstrap target {target}')
  if target in ('mp_boot_memcpy', 'mp_boot_memset') and re.search(r'\bblx?(?:\.w)?\s', function(target)):
   raise ValueError(f'Pico bootstrap function {target} calls another function')
 return [target for _, target in calls]


def audit(elf,prefix,destination,*,board='pico2w',backend=None,strict=False,irq_probe=False,nuttx_bootstrap=False):
 out=subprocess.check_output([prefix+'objdump','-d','-C',str(elf)],text=True)
 result=inspect_disassembly(out,board,backend,irq_probe)
 if nuttx_bootstrap:
  result['reset_flash_calls'] = check_nuttx_bootstrap(out)
 result['elf_sha256']=hashlib.sha256(Path(elf).read_bytes()).hexdigest()
 result['strict_gate']=strict
 Path(destination).write_text(json.dumps(result,indent=2)+'\n')
 if strict and not result['direct_paths_in_sram']:
  raise ValueError('hot-path SRAM audit failed; inspect hot-audit.json')
 return result
