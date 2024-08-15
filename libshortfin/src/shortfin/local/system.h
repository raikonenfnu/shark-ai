// Copyright 2024 Advanced Micro Devices, Inc
//
// Licensed under the Apache License v2.0 with LLVM Exceptions.
// See https://llvm.org/LICENSE.txt for license information.
// SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

#ifndef SHORTFIN_LOCAL_SYSTEM_H
#define SHORTFIN_LOCAL_SYSTEM_H

#include <memory>
#include <span>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <vector>

#include "shortfin/local/device.h"
#include "shortfin/process/worker.h"
#include "shortfin/support/api.h"
#include "shortfin/support/iree_helpers.h"

namespace shortfin::local {

class Scope;
class System;
class SystemBuilder;

// Encapsulates resources attached to the local system. In most applications,
// there will be one of these, and it is used to keep long lived access to
// physical devices, connections, and other long lived resources which need
// to be available across the application lifetime.
//
// One does not generally construct a System by hand, instead relying
// on some form of factory that constructs one to suit both the system being
// executed on and any preferences on which resources should be accessible.
//
// As the root of the hierarchy and the owner of numerous ancillary resources,
// we declare that System is always managed via a shared_ptr, as this
// simplifies many aspects of system management.
class SHORTFIN_API System : public std::enable_shared_from_this<System> {
 public:
  System(iree_allocator_t host_allocator);
  System(const System &) = delete;
  ~System();

  // Get a shared pointer from the instance.
  std::shared_ptr<System> shared_ptr() { return shared_from_this(); }

  // Access to underlying IREE API objects.
  iree_allocator_t host_allocator() { return host_allocator_; }
  iree_vm_instance_t *vm_instance() { return vm_instance_.get(); }

  // Topology access.
  std::span<const Node> nodes() { return {nodes_}; }
  std::span<Device *const> devices() { return {devices_}; }
  const std::unordered_map<std::string_view, Device *> &named_devices() {
    return named_devices_;
  }

  // Scopes.
  // Creates a new Scope bound to this System (it will internally
  // hold a reference to this instance). All devices in system order will be
  // added to the scope.
  std::unique_ptr<Scope> CreateScope();

  // Initialization APIs. Calls to these methods is only permitted between
  // construction and Initialize().
  // ------------------------------------------------------------------------ //
  void InitializeNodes(int node_count);
  void InitializeHalDriver(std::string_view moniker,
                           iree_hal_driver_ptr driver);
  void InitializeHalDevice(std::unique_ptr<Device> device);
  void FinishInitialization();

 private:
  void AssertNotInitialized() {
    if (initialized_) {
      throw std::logic_error(
          "System::Initialize* methods can only be called during "
          "initialization");
    }
  }

  const iree_allocator_t host_allocator_;

  // NUMA nodes relevant to this system.
  std::vector<Node> nodes_;

  // Map of retained hal drivers. These will be released as one of the
  // last steps of destruction. There are some ancillary uses for drivers
  // after initialization, but mainly this is for keeping them alive.
  std::unordered_map<std::string_view, iree_hal_driver_ptr> hal_drivers_;

  // Map of device name to a SystemDevice.
  std::vector<std::unique_ptr<Device>> retained_devices_;
  std::unordered_map<std::string_view, Device *> named_devices_;
  std::vector<Device *> devices_;

  // VM management.
  iree_vm_instance_ptr vm_instance_;

  // Workers.
  std::vector<std::unique_ptr<Worker>> workers_;

  // Whether initialization is complete. If true, various low level
  // mutations are disallowed.
  bool initialized_ = false;
};
using SystemPtr = std::shared_ptr<System>;

// Base class for configuration objects for setting up a System.
class SHORTFIN_API SystemBuilder {
 public:
  SystemBuilder(iree_allocator_t host_allocator)
      : host_allocator_(host_allocator) {}
  SystemBuilder() : SystemBuilder(iree_allocator_system()) {}
  virtual ~SystemBuilder() = default;

  iree_allocator_t host_allocator() { return host_allocator_; }

  // Construct a System
  virtual SystemPtr CreateSystem() = 0;

 private:
  const iree_allocator_t host_allocator_;
};

}  // namespace shortfin::local

#endif  // SHORTFIN_LOCAL_SYSTEM_H