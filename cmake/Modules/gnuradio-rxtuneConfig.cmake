find_package(PkgConfig)

PKG_CHECK_MODULES(PC_GR_RXTUNE gnuradio-rxtune)

FIND_PATH(
    GR_RXTUNE_INCLUDE_DIRS
    NAMES gnuradio/rxtune/api.h
    HINTS $ENV{RXTUNE_DIR}/include
        ${PC_RXTUNE_INCLUDEDIR}
    PATHS ${CMAKE_INSTALL_PREFIX}/include
          /usr/local/include
          /usr/include
)

FIND_LIBRARY(
    GR_RXTUNE_LIBRARIES
    NAMES gnuradio-rxtune
    HINTS $ENV{RXTUNE_DIR}/lib
        ${PC_RXTUNE_LIBDIR}
    PATHS ${CMAKE_INSTALL_PREFIX}/lib
          ${CMAKE_INSTALL_PREFIX}/lib64
          /usr/local/lib
          /usr/local/lib64
          /usr/lib
          /usr/lib64
          )

include("${CMAKE_CURRENT_LIST_DIR}/gnuradio-rxtuneTarget.cmake")

INCLUDE(FindPackageHandleStandardArgs)
FIND_PACKAGE_HANDLE_STANDARD_ARGS(GR_RXTUNE DEFAULT_MSG GR_RXTUNE_LIBRARIES GR_RXTUNE_INCLUDE_DIRS)
MARK_AS_ADVANCED(GR_RXTUNE_LIBRARIES GR_RXTUNE_INCLUDE_DIRS)
