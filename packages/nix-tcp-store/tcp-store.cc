// A `tcp://host:port` store: the daemon protocol over a TCP connection.
//
// A plain `RemoteStore`, as `ssh-ng://` is, and not a `LocalFSStore` as
// `unix://` is. `unix://` reads NAR contents from the client's own
// /nix/store, so a daemon socket forwarded from another machine accepts a
// push and fails every read (measured on 2.35.2: `nix store cat` and
// `nix copy --from` both fail with "No such file or directory").
//
// The client half of NixOS/nix#5265, ported to the authority-based store
// configuration of Nix 2.34 and later.

#include "nix/store/remote-store.hh"
#include "nix/store/remote-store-connection.hh"
#include "nix/store/store-registration.hh"
#include "nix/util/finally.hh"
#include "nix/util/url.hh"

#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cstring>

namespace nix {

// Nix 2.35 gave every store configuration a path type. The build
// defines this when the headers carry it, because the -git lane has no
// version number to compare.
#ifdef NIX_TCP_STORE_FILE_PATH_TYPE
#  define NIX_TCP_STORE_CONFIG_ARGS(params) (params), FilePathType::Unix
#else
#  define NIX_TCP_STORE_CONFIG_ARGS(params) (params)
#endif

struct TCPStoreConfig : std::enable_shared_from_this<TCPStoreConfig>, virtual RemoteStoreConfig
{
    TCPStoreConfig(const Params & params)
        : StoreConfig(NIX_TCP_STORE_CONFIG_ARGS(params))
        , RemoteStoreConfig(NIX_TCP_STORE_CONFIG_ARGS(params))
    {
    }

    TCPStoreConfig(const ParsedURL::Authority & authority, const Params & params)
        : TCPStoreConfig(params)
    {
        if (authority.host.empty())
            throw UsageError("a tcp:// store needs a host, as in 'tcp://example.org:1234'");
        if (!authority.port)
            throw UsageError("a tcp:// store needs a port, as in 'tcp://%s:1234'", authority.host);
        if (authority.user || authority.password)
            throw UsageError("a tcp:// store takes no user or password: the daemon protocol carries none");
        this->authority = authority;
    }

    ParsedURL::Authority authority;

    static const std::string name()
    {
        return "TCP Daemon Store";
    }

    static std::string doc()
    {
        return R"(
          **Store URL format**: `tcp://`*host*`:`*port*

          This store speaks the Nix daemon protocol over a TCP connection.
          The connection is neither encrypted nor authenticated. The daemon
          cannot see who connects, so it trusts as its own configuration says.
        )";
    }

    static StringSet uriSchemes()
    {
        return {"tcp"};
    }

    ref<Store> openStore() const override;

    StoreReference getReference() const override
    {
        return {
            .variant =
                StoreReference::Specified{
                    .scheme = *uriSchemes().begin(),
                    .authority = authority.to_string(),
                },
            .params = getQueryParams(),
        };
    }
};

struct TCPStore : virtual RemoteStore
{
    using Config = TCPStoreConfig;

    ref<const Config> config;

    TCPStore(ref<const Config> config)
        : Store{*config}
        , RemoteStore{*config}
        , config{config}
    {
    }

    // The daemon protocol has no operation for a build log, as
    // `SSHStore` says of the same method.
    std::optional<std::string> getBuildLogExact(const StorePath &) override
    {
        unsupported("getBuildLogExact");
    }

private:
    struct Connection : RemoteStore::Connection
    {
        AutoCloseFD fd;

        void closeWrite() override
        {
            shutdown(fd.get(), SHUT_WR);
        }
    };

    ref<RemoteStore::Connection> openConnection() override
    {
        auto & authority = config->authority;
        addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;

        addrinfo * found = nullptr;
        if (auto status = getaddrinfo(authority.host.c_str(), std::to_string(*authority.port).c_str(), &hints, &found))
            throw Error("cannot resolve '%s': %s", authority.host, gai_strerror(status));
        Finally release([&]() { freeaddrinfo(found); });

        std::string lastError = "no address";
        for (auto * candidate = found; candidate; candidate = candidate->ai_next) {
            AutoCloseFD fd = socket(candidate->ai_family, candidate->ai_socktype | SOCK_CLOEXEC, candidate->ai_protocol);
            if (!fd) {
                lastError = strerror(errno);
                continue;
            }
            if (::connect(fd.get(), candidate->ai_addr, candidate->ai_addrlen) == -1) {
                lastError = strerror(errno);
                continue;
            }
            auto conn = make_ref<Connection>();
            conn->fd = std::move(fd);
            conn->from.fd = conn->fd.get();
            conn->to.fd = conn->fd.get();
            conn->startTime = std::chrono::steady_clock::now();
            return conn;
        }
        throw Error("cannot connect to the Nix daemon at '%s': %s", authority.to_string(), lastError);
    }
};

ref<Store> TCPStoreConfig::openStore() const
{
    return make_ref<TCPStore>(ref{shared_from_this()});
}

static RegisterStoreImplementation<TCPStoreConfig> registerTCPStore;

} // namespace nix
