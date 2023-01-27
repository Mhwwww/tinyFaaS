module.exports = (req, res) => {

	console.log(req.body)
	console.log(typeof req.body)
	console.log(JSON.parse(JSON.stringify(req.body)))

	res.send(req.body)

}