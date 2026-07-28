set(CMAKE_AUTOMOC ON)

add_library(benchymark main.cpp benchlog.h)
set_target_properties(benchymark PROPERTIES PREFIX "")

asteroid_app_qml_module(benchymark
	main.qml
	benchy-mesh.js
	benchy-mesh-lite.js
)

target_link_libraries(
	benchymark
	PUBLIC
		Asteroid::AsteroidApp6
)

install(
	TARGETS benchymark
	DESTINATION ${CMAKE_INSTALL_LIBDIR}
)
