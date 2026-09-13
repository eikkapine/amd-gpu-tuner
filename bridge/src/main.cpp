// VoltShift bridge daemon — entry point.
//
// Protocol: one JSON object per line on stdin, one JSON object per line on
// stdout. Requests: {"id":<any>,"cmd":"<name>","args":{...}}. Responses echo
// the id: {"id":...,"ok":true,"data":{...}} or {"id":...,"ok":false,"error":"..."}.
// The daemon exits on EOF, "quit", or a broken stdout pipe — never on a
// failed command.
//
// Debug convenience: `voltshift_bridge.exe <cmd> [json-args]` runs a single
// command and prints the response, e.g. `voltshift_bridge.exe metrics`.

#include <exception>
#include <iostream>
#include <string>

#include "rpc.h"

using namespace voltshift;

namespace {

json Dispatch(Registry& registry, Session& session, const std::string& cmd, const json& args)
{
    auto it = registry.find(cmd);
    if (it == registry.end())
        throw BridgeError("Unknown command: " + cmd);
    return it->second(session, args);
}

json HandleLine(Registry& registry, Session& session, const std::string& line)
{
    json response;
    json id = nullptr;
    try
    {
        json request = json::parse(line);
        if (!request.is_object())
            throw BridgeError("Request must be a JSON object");
        if (request.contains("id"))
            id = request["id"];
        std::string cmd = request.at("cmd").get<std::string>();
        json args = request.value("args", json::object());
        if (!args.is_object())
            throw BridgeError("Command args must be a JSON object");

        if (cmd == "quit")
        {
            response = {{"id", id}, {"ok", true}, {"data", {{"bye", true}}}};
            return response;
        }
        response = {{"id", id}, {"ok", true}, {"data", Dispatch(registry, session, cmd, args)}};
    }
    catch (const std::exception& e)
    {
        response = {{"id", id}, {"ok", false}, {"error", e.what()}};
    }
    return response;
}

}  // namespace

int main(int argc, char* argv[])
{
    // Unbuffered pipes: the Python client reads responses line by line.
    std::ios::sync_with_stdio(false);

    Registry registry;
    RegisterCore(registry);
    RegisterTuning(registry);
    RegisterGfx(registry);
    RegisterDisplay(registry);
    RegisterExtras(registry);

    Session session;
    try
    {
        session.Initialize();
    }
    catch (const std::exception& e)
    {
        std::cout << json{{"id", nullptr}, {"ok", false}, {"error", e.what()}}.dump() << std::endl;
        return 1;
    }

    int exitCode = 0;
    if (argc > 1)
    {
        // One-shot mode for manual testing.
        json response;
        try
        {
            json request = {{"id", 0}, {"cmd", argv[1]},
                            {"args", argc > 2 ? json::parse(argv[2]) : json::object()}};
            response = HandleLine(registry, session, request.dump());
        }
        catch (const std::exception& e)
        {
            response = {{"id", 0}, {"ok", false}, {"error", e.what()}};
        }
        std::cout << response.dump(2) << std::endl;
        exitCode = response["ok"].get<bool>() ? 0 : 1;
    }
    else
    {
        // Announce readiness so the client can wait for a healthy start.
        std::cout << json{{"event", "ready"}, {"version", VOLTSHIFT_BRIDGE_VERSION}}.dump() << std::endl;

        std::string line;
        while (std::getline(std::cin, line))
        {
            if (line.empty())
                continue;
            json response = HandleLine(registry, session, line);
            std::cout << response.dump() << std::endl;
            if (!std::cout)
                break;  // client is gone
            if (response.value("ok", false) && response["data"].value("bye", false))
                break;
        }
    }

    session.Terminate();
    return exitCode;
}
