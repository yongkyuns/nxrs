//! A host-only Matter On/Off light backed by the controller tutorial's LED.
#![forbid(unsafe_code)]

use std::fs::{self, DirBuilder, File, OpenOptions};
use std::io;
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};

use async_signal::{Signal, Signals};
use embassy_time::{Instant, Timer};
use futures_lite::{StreamExt, future};
use led::Led;
use nxrs_controller_demo::{Command, Controller, State, SubmitError};
use rand::{Rng, rngs::StdRng};
use rs_matter::basic_info::BasicInfoConfig;
use rs_matter::crypto::{Crypto, default_crypto};
use rs_matter::dm::clusters::app::on_off;
use rs_matter::dm::clusters::desc::{self, ClusterHandler as _};
use rs_matter::dm::clusters::groups::{self, ClusterHandler as _};
use rs_matter::dm::clusters::identify;
use rs_matter::dm::devices::DEV_TYPE_ON_OFF_LIGHT;
use rs_matter::dm::devices::test::{DAC_PRIVKEY, TEST_DEV_ATT, TEST_DEV_DET};
use rs_matter::dm::endpoints::ROOT_ENDPOINT_ID;
use rs_matter::dm::networks::{SysNetifs, eth::EthNetwork};
use rs_matter::dm::{
    Async, Cluster, DataModel, Dataver, EmptyHandler, Endpoint, HandlerContext, ImStats,
    InvokeContext, Node, ReadContext,
};
use rs_matter::error::{Error, ErrorCode};
use rs_matter::pairing::{
    DiscoveryCapabilities, Spake2pVerifierPassword, Spake2pVerifierPasswordRef,
    qr::{CommFlowType, Qr, QrPayload, QrTextType, no_optional_data},
};
use rs_matter::persist::DirKvBlobStore;
use rs_matter::stack::MatterStack;
use rs_matter::stack::eth::PreexistingEthernet;
use rs_matter::stack::net::std::StdNetStack;
use rs_matter::transport::network::mdns::astro::AstroMdns;
use rs_matter::utils::init::InitMaybeUninit;
use rs_matter::{BasicCommData, DevCommData, Matter, clusters, devices, root_endpoint, with};
use static_cell::StaticCell;

const LIGHT: u16 = 1;
const PAIRING_SECONDS: u16 = 180;
type Stack = MatterStack<'static, { 64 * 1024 }, EthNetwork<'static>>;
static STACK: StaticCell<Stack> = StaticCell::new();

const DEVICE: BasicInfoConfig<'static> = BasicInfoConfig {
    product_name: "NXRS Simulated LED",
    device_name: "NXRS Simulated LED",
    ..TEST_DEV_DET
};

const ON_OFF: Cluster<'static> = on_off::FULL_CLUSTER
    .with_features(0)
    .with_attrs(with!(required))
    .with_cmds(with!(
        on_off::CommandId::On | on_off::CommandId::Off | on_off::CommandId::Toggle
    ));
const NODE: Node<'static> = Node {
    endpoints: &[
        root_endpoint!(eth),
        Endpoint::new(
            LIGHT,
            devices!(DEV_TYPE_ON_OFF_LIGHT),
            clusters!(
                desc::DescHandler::CLUSTER,
                groups::GroupsHandler::CLUSTER,
                identify::CLUSTER,
                ON_OFF
            ),
        ),
    ],
};

struct Light<'a, L> {
    controller: &'a Controller<L>,
    version: Dataver,
}

impl<L: Led + Send + 'static> Light<'_, L> {
    // The tutorial controller owns the output. Report success only after its
    // completion ticket confirms the command. There is no second LED state.
    fn command(&self, command: Command) -> Result<bool, Error> {
        let ticket = self
            .controller
            .try_command(command)
            .map_err(|error| match error {
                SubmitError::Full(_) => ErrorCode::Busy,
                SubmitError::Disconnected(_) => ErrorCode::InvalidInState,
            })?;
        match ticket.wait().map_err(|_| ErrorCode::InvalidInState)? {
            Ok(State::On) => Ok(true),
            Ok(State::Off) => Ok(false),
            _ => Err(ErrorCode::Failure.into()),
        }
    }

    fn set(&self, on: bool, ctx: impl InvokeContext) -> Result<(), Error> {
        let state = self.command(Command::Set(on));
        self.version.changed();
        ctx.notify_own_attr_changed(on_off::AttributeId::OnOff as _);
        let reported = match &state {
            Ok(true) => "on",
            Ok(false) => "off",
            Err(_) => "unavailable",
        };
        println!("LED {reported}");
        state.map(|_| ())
    }
}

impl<L: Led + Send + 'static> on_off::ClusterHandler for Light<'_, L> {
    const CLUSTER: Cluster<'static> = ON_OFF;

    fn dataver(&self) -> u32 {
        self.version.get()
    }

    fn dataver_changed(&self) {
        self.version.changed();
    }

    fn on_off(&self, _ctx: impl ReadContext) -> Result<bool, Error> {
        self.command(Command::Status)
    }

    async fn run(&self, ctx: impl HandlerContext) -> Result<(), Error> {
        let stats = ctx.im_stats();
        // The SDK schedules the next keep-alive after the subscriber receives the
        // initial report. A restored subscription is immediately due until then.
        while !stats
            .next_keep_alive_at()
            .is_some_and(|deadline| deadline > Instant::now())
        {
            Timer::after_millis(100).await;
        }
        println!("Ready to switch the light.");
        future::pending().await
    }

    fn handle_on(&self, ctx: impl InvokeContext) -> Result<(), Error> {
        self.set(true, ctx)
    }

    fn handle_off(&self, ctx: impl InvokeContext) -> Result<(), Error> {
        self.set(false, ctx)
    }

    fn handle_toggle(&self, ctx: impl InvokeContext) -> Result<(), Error> {
        self.set(!self.command(Command::Status)?, ctx)
    }

    fn handle_off_with_effect(
        &self,
        _ctx: impl InvokeContext,
        _request: on_off::OffWithEffectRequest<'_>,
    ) -> Result<(), Error> {
        Err(ErrorCode::CommandNotFound.into())
    }

    fn handle_on_with_recall_global_scene(&self, _ctx: impl InvokeContext) -> Result<(), Error> {
        Err(ErrorCode::CommandNotFound.into())
    }

    fn handle_on_with_timed_off(
        &self,
        _ctx: impl InvokeContext,
        _request: on_off::OnWithTimedOffRequest<'_>,
    ) -> Result<(), Error> {
        Err(ErrorCode::CommandNotFound.into())
    }
}

fn model<'a, L: Led + Send + 'static>(
    matter: &'a Matter<'a>,
    mut rand: impl Rng + Copy,
    controller: &'a Controller<L>,
) -> impl DataModel + 'a {
    (
        NODE,
        EmptyHandler
            .chain(
                |endpoint, _| endpoint == ROOT_ENDPOINT_ID,
                Async(Stack::root_handler(matter, &(), rand)),
            )
            .chain(
                |endpoint, cluster| endpoint == LIGHT && cluster == desc::DescHandler::CLUSTER.id,
                Async(desc::DescHandler::new(Dataver::new_rand(&mut rand)).adapt()),
            )
            .chain(
                |endpoint, cluster| {
                    endpoint == LIGHT && cluster == groups::GroupsHandler::CLUSTER.id
                },
                Async(groups::GroupsHandler::new(Dataver::new_rand(&mut rand), matter).adapt()),
            )
            .chain(
                |endpoint, cluster| endpoint == LIGHT && cluster == identify::CLUSTER.id,
                Async(identify::IdentifyHandler::new(Dataver::new_rand(&mut rand)).adapt()),
            )
            .chain(
                |endpoint, cluster| endpoint == LIGHT && cluster == ON_OFF.id,
                Async(on_off::HandlerAdaptor(Light {
                    controller,
                    version: Dataver::new_rand(&mut rand),
                })),
            ),
    )
}

fn state_lock(path: &Path) -> io::Result<File> {
    match DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => (),
        Err(error) if error.kind() == io::ErrorKind::AlreadyExists => (),
        Err(error) => return Err(error),
    }
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir() || metadata.permissions().mode() & 0o077 != 0 {
        return Err(io::Error::other(
            "use a private state directory with mode 0700",
        ));
    }
    let lock = OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path.join("owner.lock"))?;
    lock.try_lock().map_err(io::Error::other)?;
    Ok(lock)
}

fn print_pairing_codes(pairing: BasicCommData) -> Result<(), Error> {
    let manual_code = pairing.compute_pretty_pairing_code();
    let payload = QrPayload::new(
        DiscoveryCapabilities::IP,
        CommFlowType::Standard,
        pairing,
        DEVICE.vid,
        DEVICE.pid,
        DEVICE.serial_no,
        no_optional_data,
    );
    let text = payload.emit_chars().collect::<Result<String, Error>>()?;
    let mut scratch = [0; 512];
    let mut matrix = [0; 512];
    let qr = Qr::compute(&text, &mut scratch, &mut matrix)?;
    let image = qr
        .emit_chars(QrTextType::Unicode, 4, false)
        .collect::<String>();
    println!("SetupQRCode: [{text}]");
    println!("PairingCode: [{manual_code}]");
    print!("{image}");
    Ok(())
}

async fn serve<L: Led + Send + 'static>(
    controller: &Controller<L>,
    path: PathBuf,
    pair: bool,
) -> Result<(), Box<dyn std::error::Error>> {
    // These eight-digit values avoid Matter's reserved repeated and sequential codes.
    let passcode = rand::random_range(80_000_000..87_000_000_u32);
    let pairing = BasicCommData {
        password: Spake2pVerifierPassword::new_from_ref(Spake2pVerifierPasswordRef::new(
            &passcode.to_le_bytes(),
        )),
        discriminator: rand::random_range(0..4096),
    };
    let stack = STACK.uninit().init_with(MatterStack::init(
        &DEVICE,
        DevCommData::Passcode(pairing.clone()),
        &TEST_DEV_ATT,
    ));
    let crypto = default_crypto(rand::make_rng::<StdRng>(), DAC_PRIVKEY);
    let kv = stack.kv(DirKvBlobStore::new(path));
    stack.matter().startup(&kv)?;
    if !stack.matter().has_fabrics() {
        if !pair {
            return Err("no Matter enrollment yet. Run with --pair to add the LED".into());
        }
        let matter = stack.matter();
        matter.open_basic_comm_window(PAIRING_SECONDS, &crypto, &())?;
        print_pairing_codes(pairing)?;
    }
    println!("LED off");
    println!("Waiting for the Matter controller.");
    let mut signals = Signals::new([Signal::Int, Signal::Term])?;
    future::race(
        async {
            let rand = crypto.rand()?;
            stack
                .run_eth(
                    PreexistingEthernet::new(StdNetStack::new(), SysNetifs, &(), AstroMdns::new()),
                    &crypto,
                    model(stack.matter(), rand, controller),
                    kv,
                    (),
                )
                .await?;
            Ok(())
        },
        async {
            signals.next().await.transpose()?;
            Ok(())
        },
    )
    .await
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut args = std::env::args_os().skip(1);
    let usage = "usage: NXRS_PLATFORM=mock nxrs-matter-led STATE_DIRECTORY [--pair]";
    let path = PathBuf::from(args.next().ok_or(usage)?);
    let pair = match args.next() {
        Some(flag) if flag == "--pair" => true,
        None => false,
        _ => return Err(usage.into()),
    };
    if args.next().is_some() {
        return Err(usage.into());
    }
    let _lock = state_lock(&path)?;
    let output = led::open().map_err(|error| format!("cannot open LED: {error:?}"))?;
    let controller = Controller::start(output, 0)?;
    let result = future::block_on(serve(&controller, path, pair));
    let stopped = controller
        .stop()
        .map_err(|_| "controller worker panicked")?;
    stopped
        .shutdown
        .map_err(|error| format!("cannot turn LED off: {error:?}"))?;
    println!("Stopped. LED off");
    result
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    env_logger::init_from_env(env_logger::Env::default().default_filter_or("info"));
    run()
}
